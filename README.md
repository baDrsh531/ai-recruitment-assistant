# AI Recruitment Assistant

Plateforme RH d'analyse de candidatures : extraction structuree des CV,
score de compatibilite avec une offre, classement et aide a la decision.

> **Ce qui distingue ce projet d'un simple wrapper LLM**
>
> 1. **Le score est deterministe.** Il est calcule par un moteur explicite a
>    partir de poids configurables. Le modele de langage n'attribue aucune
>    note — il explique un resultat deja calcule. Deux executions donnent le
>    meme score.
> 2. **Rien n'est extrait sans preuve.** Chaque competence, experience ou
>    diplome pointe vers un extrait verbatim du CV, avec sa page et ses
>    coordonnees. Une affirmation que l'on ne retrouve pas dans le document
>    est rejetee.
> 3. **Tout est mesure.** Un harnais d'evaluation sur jeu annote (nDCG@5,
>    precision d'extraction, latence p50/p95) tourne en integration continue :
>    changer un prompt ou un modele se traduit par des chiffres, pas par une
>    impression.
> 4. **Conforme par conception.** Le tri de CV est un systeme d'IA a haut
>    risque (AI Act, annexe III.4). Journal d'audit immuable, supervision
>    humaine obligatoire, versionnage des prompts, mode screening a l'aveugle
>    et purge RGPD sont dans le modele de donnees, pas en annexe.

---

## Architecture

```
                  Navigateur (templates Django + Tailwind)
                                   |
                          Django 5 · DRF · vues
                                   |
        +--------------------------+---------------------------+
        |                          |                           |
   PostgreSQL / SQLite      Celery + Redis              Couche IA (apps/ai)
   donnees metier           extraction async         +----------+-----------+
   journal d'audit          scoring differe          |                      |
                                                Qwen3.6 35B          Qwen3-VL 8B
                                                :30000/v1             :30001/v1
                                                texte, JSON Schema    pages en image
                                                                             |
                                                          Embeddings (fastembed ONNX)
                                                          recherche vectorielle numpy
```

**Pourquoi pas de base vectorielle dediee.** Sous 50 000 CV, un produit
scalaire numpy sur vecteurs normalises prend quelques millisecondes. Ajouter
ChromaDB ou Qdrant serait un service de plus sans gain mesurable. L'interface
de `apps/ai/embeddings.py` isole ce choix : basculer vers pgvector ne touchera
que ce fichier. Meme raisonnement pour la recherche plein texte : l'index BM25
est reconstruit en memoire a chaque requete. Au-dela de quelques dizaines de
milliers de profils c'est PostgreSQL full-text ou un moteur dedie qu'il faudrait
— la limite est connue, elle n'est pas franchie ici.

**Pourquoi deux modeles.** Le modele texte traite les CV a texte natif. Le
modele vision lit les pages **en image** : c'est ce qui permet de traiter les
CV multi-colonnes, les encarts lateraux, les tableaux et les PDF scannes — le
point le plus difficile du projet, et celui ou la plupart des parsers echouent.

---

## Demarrage

Le plus simple, sous Windows :

```
start.bat            demarre sur le port 4040
start.bat 8000       autre port
```

Le script cree l'environnement virtuel, installe les dependances, copie le
`.env` et applique les migrations si necessaire.

Installation manuelle :

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

copy .env.example .env        # puis ajuster LLM_BASE_URL / VLM_BASE_URL
python manage.py migrate
python manage.py seed_demo    # jeu de demonstration
python manage.py runserver 4040
```

Interface sur http://127.0.0.1:4040/ — deux comptes de demonstration, meme mot
de passe `demo-recrutement-2026` :

| Compte | Role | Ce qu'il peut faire |
|---|---|---|
| `recruteur` | recruteur | tout : deposer, scorer, decider |
| `observateur` | lecture seule | consulter ; toute action est refusee et journalisee |

Le second existe pour verifier que le controle d'acces fait quelque chose.

### Verifier la connexion au serveur d'inference

```powershell
python manage.py check_ai
```

La commande liste les **identifiants exacts** des modeles exposes par chaque
endpoint (a recopier dans le `.env`), teste le decodage contraint par JSON
Schema et mesure la latence reelle.

En cas de timeout, le probleme est reseau avant d'etre applicatif. Dans
l'ordre :

```powershell
Test-NetConnection 192.168.0.64 -Port 30000    # le port repond-il ?
Get-NetRoute -AddressFamily IPv4 | Where-Object DestinationPrefix -like "192.168.*"
```

Un poste sur `192.168.1.x` n'a aucune route vers `192.168.0.x` : les NodePort
Kubernetes ne sortent pas du reseau du cluster. Cote cluster, `kubectl get
nodes -o wide` donne l'adresse reelle des noeuds et `kubectl get svc -A |
grep NodePort` les ports effectivement publies.

### Mesures relevees sur le serveur reel

Qwen3.6-35B (texte) et Qwen3-VL-8B (vision), servis en GGUF par llama.cpp.

| Operation | Duree | Tokens generes |
|---|---|---|
| Extraction d'un CV, voie texte | 10 s | 1121 |
| Extraction d'un CV, voie vision | 11 s | — |
| Analyse redigee d'un score | 6,6 s | 827 |
| Score de compatibilite (moteur seul) | 0,1 s | 0 |

**Qwen3.6 est un modele a raisonnement.** Il ecrit sa reflexion dans
`reasoning_content` avant de produire sa reponse : 390 tokens de reflexion
contre 25 de reponse sur une question simple, soit **seize fois le cout** pour
un resultat identique. Le projet desactive donc le raisonnement par defaut
(`chat_template_kwargs: {"enable_thinking": false}`), et l'active
explicitement la ou une chaine de raisonnement apporte quelque chose.

Deux consequences pratiques, apprises en confrontant le code au vrai serveur :

- un budget de tokens trop court fait rendre un **contenu vide** avec
  `finish_reason: length`. Le client detecte ce cas et le signale comme une
  reponse tronquee, au lieu de laisser remonter une erreur de JSON malforme
  qui envoyait chercher le probleme au mauvais endroit ;
- les identifiants de modele exposes par llama.cpp sont des **chemins de
  fichiers** (`E:\vllm_models\gguf\...gguf`). `check_ai` les affiche pour
  qu'ils soient recopies tels quels dans le `.env`.

### Travailler sans serveur d'inference

```powershell
python manage.py mock_inference --port 30000     # modele texte
python manage.py mock_inference --port 30001     # modele vision
```

Un serveur compatible OpenAI qui **n'est pas un modele** : il repond au
protocole avec des donnees fabriquees par des regles. Il existe parce que la
couche reseau etait la seule partie du projet jamais exercee pour de vrai —
tout le reste est teste avec l'appel modele simule.

Ce qu'il permet de valider : decodage contraint par JSON Schema, repli sur
`guided_json` pour les serveurs qui ignorent `response_format`, reprise sur
erreur, envoi d'images en base64, journalisation des appels, et le pipeline
d'extraction de bout en bout. Ce qu'il ne dit pas : la qualite de l'extraction
reelle, qui depend du modele.

Deux modes degrades sont simulables, pour eprouver la robustesse du client :

```powershell
python manage.py mock_inference --fail-rate 0.4          # 40 % de 503
python manage.py mock_inference --reject-response-format # repli guided_json
```

### Extraire un CV en ligne de commande

```powershell
python manage.py parse_cv chemin\vers\cv.pdf
python manage.py parse_cv chemin\vers\cv.pdf --no-llm   # diagnostic seul
```

`--no-llm` s'arrete apres l'extraction du texte et affiche le diagnostic de
mise en page — utile pour verifier la detection multi-colonnes ou scan sans
serveur d'inference joignable. Exemple sur deux PDF au contenu identique mais
de mise en page differente :

```
== cv_deux_colonnes.pdf ==        == cv_une_colonne.pdf ==
Caracteres      951 (951.0/page)  Caracteres      951 (951.0/page)
Scan presume    non               Scan presume    non
Multi-colonnes  pages [1]         Multi-colonnes  non
Voie retenue    vision (Qwen3-VL) Voie retenue    texte (Qwen3.6)
```

### Classer les candidats d'une offre

```powershell
python manage.py score_offer <slug> --no-explain          # moteur seul
python manage.py score_offer <slug> --no-explain --detail # detail par critere
```

`--no-explain` n'utilise que le moteur deterministe : aucun serveur
d'inference requis, et le resultat est identique a chaque execution.

```
Rg  Candidat                      Score   Ecarts
1   Ahmed Benali                    90 %   -
2   Badr Sahraoui                   85 %   PostgreSQL
3   Sara El Amrani                  75 %   Django, PostgreSQL

Moteur           1.0.0
Semantique       indisponible (ontologie seule)
Calcul           175 ms au total · median 4 ms · max 168 ms
```

Chaque ligne est justifiable :

```
-- Sara El Amrani (75 %) --
  Competences        59 %  (poids 47 %)
  Experience        100 %  (poids 21 %)
  Certifications      -    non applicable (aucune certification exigee)
    · Python                  100 %  Python [exact]
    · Django                   45 %  Python [ontologie]      <- prerequis seul
    · PostgreSQL               45 %  SQL [ontologie]
```

### Mesurer la qualite du classement

```powershell
python manage.py evaluate --detail
python manage.py evaluate --baseline apps/evaluation/baselines/ranking_v1.json
```

Le jeu `ranking_v1` contient sept offres et 38 candidats, chacun note a la main
de 0 a 3. Le harnais reconstruit ces cas en base, fait tourner le moteur,
compare le classement produit au classement attendu, puis **annule tout** :
evaluer ne laisse aucune trace.

```
Cas                             nDCG@5       P@3    Paires  Spearman
backend_python_senior            1.000     1.000     1.000     0.971
arbitrage_competences_anciennete 0.979     1.000     0.889     0.821
profils_proches                  1.000     1.000     1.000     0.894
---------------------------------------------------------------------
Moyenne                          0.997     1.000     0.984     0.937
```

`--baseline` affiche l'ecart avec un rapport de reference et renvoie un code
d'erreur en cas de regression : la CI echoue avec les chiffres a l'appui.

### Graphiques

Deux tableaux de bord : l'un RH (competences, anciennete, langues,
distribution des scores), l'autre sur le **cout et la latence des appels
modele** — latence mediane et 95e centile par usage, tokens d'entree et
generes, volume quotidien.

```powershell
node scripts/audit_charts.mjs
```

Les graphiques sont du **SVG genere dans le navigateur, sans aucune
bibliotheque**. Trois choix structurent le rendu :

- **Le tableau equivalent est toujours emis par le serveur.** Sans JavaScript,
  la page reste lisible et chaque valeur reste accessible ; avec, le tableau
  cede la place au graphique et revient d'un clic. L'infobulle enrichit, elle
  ne conditionne jamais l'acces a une valeur.
- **La palette est validee, pas choisie a l'oeil.** Les deux teintes
  categorielles passent tous les controles sur les surfaces reelles du projet,
  en clair comme en sombre : ecart CVD 24.7 et 26.8 (cible >= 8), ecart en
  vision normale 33.6 et 31.8 (plancher 15), contraste >= 3:1. Une serie unique
  recoit une seule couleur — jamais un degrade qui redirait la longueur de la
  barre.
- **La geometrie est auditee.** `scripts/audit_charts.mjs` rejoue le rendu avec
  un DOM minimal sur des cas limites — intitule tres long, valeur a six
  chiffres, courbe a deux points — et echoue si quoi que ce soit sort du cadre.
  Il a trouve trois debordements reels que les donnees de demonstration ne
  faisaient jamais apparaitre. Il tourne en integration continue.

### Une couche mesuree, puis desactivee : le rapprochement semantique

Le moteur prevoit un troisieme niveau de rapprochement des competences, apres
la correspondance exacte et l'ontologie : la similarite d'embeddings, censee
attraper les intitules que l'ontologie ne connait pas. Il est **desactive par
defaut**, et la commande suivante dit pourquoi :

```powershell
python manage.py probe_semantic
```

```
Offre                     Candidat                Attendu         cosinus
Symfony                   Laravel                 proches           0.393
Polars                    Pandas                  proches           0.332
Rust                      Go                      proches           0.326
Symfony                   Comptabilite            sans rapport      0.647
Kubernetes                Boulangerie             sans rapport      0.827

paire proche la moins bien notee   : 0.280
paire sans rapport la mieux notee  : 0.827
-> les deux populations se chevauchent, aucun seuil ne les separe.
```

Un modele de phrases generaliste n'a **aucune connaissance technique**. Il note
« Kubernetes / Boulangerie » au-dessus de toutes les paires reellement
proches, et cette paire franchit le seuil : activee, la couche crediterait un
boulanger sur une exigence Kubernetes.

Le harnais de classement confirme l'inutilite par un autre chemin : active ou
non, les quatre metriques sont **rigoureusement identiques** (nDCG@5 0.997).

Le code reste en place et l'option `EMBEDDING_PROVIDER=local` le reactive : il
redeviendra pertinent avec un modele entraine sur une taxonomie de
competences. En attendant, l'ontologie fait le travail — et elle a le merite
d'etre inspectable ligne a ligne.

### Mesurer la qualite de l'extraction

```powershell
python manage.py evaluate_extraction --detail
```

Le protocole evite l'annotation manuelle : chaque CV est **genere a partir
d'un profil structure**, puis le systeme est mis au defi de reconstituer ce
profil. La verite terrain est donc exacte par construction. Quatre mises en
page eprouvent des voies differentes du pipeline.

```
Cas                     Mise en page   Voie     Identite  Comp. F1  Langues  Preuves
backend_python          simple         text         1.00      1.00     1.00   7/7
data_engineer_colonnes  deux_colonnes  hybrid       1.00      1.00     1.00  11/11
frontend_tableau        tableau        text         1.00      0.46     1.00   8/8
ml_engineer_scanne      scanne         vision       1.00      1.00     1.00   n/a
devops_colonnes         deux_colonnes  hybrid       1.00      1.00     1.00  12/12

Identite 1.000 · Competences F1 0.892 · Langues 1.000 · Preuves 1.000
Erreur d'anciennete 1.18 an · 14 s par CV
```

Cette commande **appelle reellement le modele** : elle demande un serveur
d'inference joignable et ne tourne pas en integration continue.

**`n/a` sur le CV scanne n'est pas un echec.** Un document sans couche texte
n'offre rien a quoi confronter les citations du modele : les donnees n'y sont
pas contredites, elles sont **inverifiables**. La distinction figure dans le
calcul comme a l'ecran.

**`frontend_tableau` a 0.46 est une faiblesse reelle**, laissee visible : sur
une mise en page ou les competences sont dans des cellules, le modele en
manque trois et range les langues parmi les competences. Retirer ce cas du jeu
ferait remonter la moyenne sans rien ameliorer.

### Questions d'entretien ancrees dans le CV

Depuis la page d'une candidature, un bouton genere un jeu de questions. La
regle : **chaque question vise une affirmation precise du profil**, et le
champ `cv_claim` la conserve — c'est ce qui distingue une question preparee
d'un questionnaire qu'on peut reciter.

```
1. [Verifier un acquis annonce] Integration LLM
   Dans le cadre de l'integration des LLM pour le traitement documentaire,
   comment avez-vous gere la latence et les erreurs lors des appels API ?
   ancree sur : Integration de modeles de langage pour l'automatisation
                du traitement documentaire
   bonne reponse : gestion des timeouts, retry, validation des sorties,
                   fallback, suivi des couts
```

Comme pour l'analyse du score, le modele ne recoit **jamais le CV brut** : il
travaille sur le profil deja extrait et les ecarts deja calcules. Le nom du
candidat ne lui est pas transmis. Chaque question conserve la version du
prompt et le modele qui l'a produite.

Limite constatee : le modele varie peu les intentions et produit surtout des
questions de verification, malgre la consigne. Les mises en situation
demandent d'insister.

### Auditer les biais

```powershell
python manage.py audit_bias --detail
```

La question posee est celle d'un auditeur : *si cette meme personne avait un
autre prenom, une autre ville, un autre age apparent, serait-elle toujours
retenue ?* Pour chaque candidat du jeu annote, on produit des variantes ne
differant que par un attribut identitaire, puis on rescore.

```
Attribut              Ecart moyen  Ecart max  Rangs modifies  Ratio impact
prenom_et_nom             0.00000    0.00000         0 / 180         1.000
localisation              0.03012    0.07900        22 / 108         0.809
annee_de_diplome          0.00000    0.00000         0 / 108         1.000
etablissement             0.00000    0.00000         0 / 108         1.000
```

Le **ratio d'impact** compare le taux de selection en tete de classement entre
la variante la moins retenue et la mieux retenue. Le seuil de 0.80 est la regle
dite des quatre cinquiemes, reprise par la loi new-yorkaise LL144 sur l'audit
des outils automatises d'aide au recrutement. La commande renvoie un code
d'erreur en dessous : la CI echoue.

Ce que ces chiffres etablissent :

- le prenom, le nom, l'annee de diplome et l'etablissement **n'ont aucun effet
  mesurable** — ce n'est pas une intention affichee, c'est verifie a chaque
  execution sur 500 comparaisons ;
- la **localisation est le seul levier identitaire actif**, avec un ratio de
  0.809, juste au-dessus du seuil. C'est un critere metier legitime pour un
  poste sur site, mais aussi un marqueur social. Deux attenuations sont
  prevues : l'exclure en mode aveugle, et remplacer la comparaison textuelle
  par un geocodage.

Deux proprietes de non-discrimination sont verifiees en plus : le score ne
depend jamais du nom, et ajouter quinze ans d'anciennete ne le fait jamais
baisser (penaliser la surqualification serait un critere d'age indirect).

### Attenuer : le screening a l'aveugle

```powershell
python manage.py audit_bias --compare-blind
```

Le mode aveugle s'active **offre par offre** — la politique appartient au
poste, pas au recruteur, sinon deux personnes verraient deux classements
differents pour la meme offre. Il exclut la localisation du calcul et masque
les employeurs dans l'analyse redigee : un nom d'entreprise renseigne sur le
milieu et le reseau, sans rien dire de la competence.

```
Attribut              Ratio standard  Ratio aveugle     Gain    Rangs modifies
localisation                   0.809          1.000   +0.191          22 -> 0
prenom_et_nom                  1.000          1.000   +0.000           0 -> 0
annee_de_diplome               1.000          1.000   +0.000           0 -> 0
etablissement                  1.000          1.000   +0.000           0 -> 0
```

L'effet n'est pas reduit, il est **neutralise** : ecart maximal 0.079 -> 0.000.
Sur l'offre de demonstration :

```
== STANDARD ==                      == AVEUGLE ==
1. Ahmed Benali    96 % (Casablanca)  1. Ahmed Benali    95 % (Casablanca)
2. Badr Sahraoui   85 % (Rabat)       2. Badr Sahraoui   90 % (Rabat)
3. Sara El Amrani  75 % (Tanger)      3. Sara El Amrani  78 % (Tanger)
```

L'ecart entre le premier et le deuxieme passe de 11 a 5 points, et les
candidats eloignes remontent.

**Contrepartie assumee** : la contrainte geographique disparait du calcul. Pour
un poste sur site, elle devra etre reintroduite plus tard dans le processus,
par une decision humaine tracee — jamais par un tri automatique.

Les memes chiffres sont consultables par le recruteur sur la page
**Transparence** de l'application.

### Gouvernance : trois affirmations rendues verifiables

Le projet annoncait trois garanties que le code ne tenait pas. Un `grep` l'a
montre sans ambiguite : `decide()` n'etait appelee par aucune vue, `can_decide`
n'etait teste nulle part, `retention_until` restait vide sur les six dossiers en
base. Trois champs de modele et un principe affiche — mais aucun comportement.

**1. La decision est humaine, motivee et imputee.** La page d'une candidature
porte un selecteur d'etape et un champ de motif. Ecarter un candidat
(`rejected`, `withdrawn`) exige un motif d'au moins dix caracteres : le score ne
motive pas une decision, le recruteur si. Le dossier ne conserve que la derniere
decision ; le journal d'audit les conserve toutes, avec leur auteur et leur
horodatage.

**2. Le role est applique, pas seulement declare.** Un `ActionPermissionMixin`
sur chaque vue qui ecrit — decision, calcul de score, generation de questions,
depot de CV, assistant, rapport de biais — refuse l'action a un compte en
lecture seule, et **journalise le refus** : une tentative refusee interesse un
auditeur autant qu'une action reussie. La verification est doublee dans
`decide()`, pour qu'appeler le service directement ne contourne rien. Le compte
de demonstration `observateur` permet de le constater.

**3. La conservation a une echeance et elle est respectee.** Chaque dossier
recoit une date de fin de conservation a la creation. `purge_expired` supprime
en cascade CV, profil, preuves, scores et questions ; `--dry-run` montre ce qui
partirait avant de detruire quoi que ce soit. Une tache Celery quotidienne fait
la meme chose. Le journal garde le compte des dossiers supprimes et leurs
identifiants — **jamais un nom, jamais une adresse** — et c'est precisement ce
qui permet de prouver la purge sans conserver ce qu'elle a efface.

Le detail qui aurait tout annule : les dossiers deja en base avaient un champ
vide, et `retention_until__lt` ne selectionne jamais un NULL. Ils auraient ete
conserves indefiniment, en silence, par la fonctionnalite meme censee
l'empecher. Une migration de donnees leur donne une echeance calculee depuis
leur date de creation reelle — un dossier vieux d'un an ne repart pas pour une
duree complete.

```powershell
python manage.py purge_expired --dry-run   # ce qui serait supprime
python manage.py purge_expired             # suppression definitive
```

Vingt-huit tests couvrent ces trois mecanismes (`tests/test_governance.py`),
dont la verification qu'un compte en lecture seule est refuse sur *chacune* des
vues mutantes, et que le journal de purge ne contient aucune donnee nominative.

### Ce que la ponderation coute

La ponderation des criteres est le seul endroit ou un recruteur decide de ce
qui compte. Elle etait modifiable depuis l'origine, mais a l'aveugle. Le
simulateur (`/offres/<slug>/ponderation/`) rejoue le moteur avec une autre
ponderation et montre le classement obtenu **et** ce que cette ponderation fait
au ratio d'impact.

**Le resultat qui justifie le module :**

```
ponderation                              location  skills   ratio d'impact
defaut                                      0,100   0,450        0,809
skills 0,45 -> 0,20, experience 0,20 -> 0,45  0,100   0,200        0,714  ← sous le seuil
```

Baisser le poids des competences fait passer le systeme **sous le seuil des
quatre cinquiemes sans toucher au poids de la localisation**. Quand les
competences cessent de departager les candidats, ce sont les criteres restants
qui decident, localisation comprise. Un recruteur qui se dirait « je vais
valoriser l'experience plutot que les competences » franchirait le seuil sans
le savoir.

Rien n'est enregistre : la simulation passe par un parametre du moteur, jamais
par une ecriture temporaire sur l'offre.

### Surveiller, pas seulement auditer

L'audit contrefactuel est une photographie. `monitor_bias` en fait un releve :
a chaque execution les ratios sont recalcules, compares au dernier releve
enregistre, et journalises. C'est l'enregistrement qui permet de repondre a
« depuis quand ? », et un ecart apparu il y a six mois n'a pas les memes
consequences qu'un ecart apparu hier.

Deux niveaux, et la distinction est le coeur du dispositif : l'**ecart legal**
— un ratio sous 0,80 — et la **derive**, une baisse d'au moins 0,05 sans
franchissement de seuil. Le second est le signal utile : quand le premier se
declenche, il est deja tard.

Le module **ne bloque rien** en exploitation. Un systeme qui refuserait de
scorer parce qu'un ratio a baisse mettrait un recruteur devant un ecran vide
sans qu'il puisse rien y faire. `--strict` existe pour la CI, ou une derive doit
arreter le train.

### Les CV en arabe

Un CV arabe extrait d'un PDF ne rend pas les lettres qu'on croit. Le document
stocke des **formes de presentation** — les variantes contextuelles d'une lettre
selon sa position dans le mot, dans la plage U+FE70–U+FEFF. « سارة » ecrit dans
le CV ressort en « ﺱﺍﺭﺓ » : d'autres points de code, donc aucune egalite de
chaine possible avec une requete tapee au clavier.

**La mesure, sur un CV arabe genere a verite connue :**

```
                            champs retrouves sur 8
extraction brute                      2      (les deux champs latins)
apres normalisation                   7
```

Sans cette etape, un CV arabe n'est pas partiellement lisible pour le systeme :
il est illisible. La normalisation fait converger les formes de presentation
vers les lettres de base, replie les variantes graphiques d'une meme lettre
(`أ` `إ` `آ` → `ا`, `ة` → `ه`, `ى` → `ي`), retire le tatweel et les
diacritiques, et convertit les chiffres arabo-indiens. « أحمد » et « احمد »
designent la meme personne ; le rapprochement de doublons et la recherche le
savent maintenant.

**Pourquoi a la comparaison et non a l'extraction.** La normalisation change la
longueur du texte — la ligature lam-alef est un caractere qui en devient deux.
Appliquee a l'extraction, elle decalerait tous les offsets et casserait la
correspondance entre un extrait et sa bbox, sur laquelle repose tout l'ancrage
des preuves. Elle se fait donc au moment de comparer, comme le retrait des
accents.

**Le huitieme champ, et pourquoi il manque.** « الدار البيضاء » ressort en
« البيضا » : le hamza final n'est pas rendu dans le PDF. C'est une perte au
rendu, pas un defaut de normalisation — le caractere n'est pas dans le
document. Les vrais PDF arabes presentent la meme classe de perte, ce qui rend
la mesure representative plutot qu'artificiellement propre : 7 sur 8 est un
plancher.

**Ce qui n'est pas mesure, et ne doit pas etre revendique.** La voie vision sur
un CV arabe scanne n'a pas ete eprouvee — le serveur d'inference n'est pas
joignable depuis l'environnement de developpement. Et le CV de test, bien que
melangeant arabe et latin sur une meme ligne comme un vrai CV marocain, reste
plus propre qu'un document reel. Ces chiffres portent sur la couche texte,
c'est-a-dire sur ce qui alimente le modele — pas sur le modele lui-meme.

### Mesurer aussi les humains

Le projet mesure beaucoup ce que fait le moteur et jamais ce que font les gens
qui s'en servent. C'est un angle mort : un outil dont on affirme qu'il ne decide
rien repose entierement sur la qualite des decisions qu'il assiste.

`/transparence/accord/` mesure deux choses. Le **kappa de Cohen** dit si deux
recruteurs qui voient les memes dossiers prennent les memes decisions ;
l'**ecart au score** dit si un recruteur suit le classement ou s'en detache.

**Pourquoi le kappa plutot qu'un pourcentage** — c'est tout l'interet de la
mesure. Quand neuf candidatures sur dix sont ecartees, deux recruteurs qui
repondraient au hasard seraient d'accord 80 % du temps. Un test le montre : sur
un jeu ou l'accord brut vaut 0,80, le kappa est **negatif**. Le pourcentage
aurait fait croire a un consensus la ou il n'y a que la structure du vivier.

Aucune des deux positions extremes n'est bonne en soi : un recruteur qui suit
toujours le score n'apporte rien qu'un seuil automatique n'apporterait, un
recruteur qui s'en detache systematiquement rend le score inutile. **Ces
chiffres ne notent personne** — un recruteur qui s'ecarte du score peut avoir
raison, il a vu le candidat quand le score a vu un PDF. En dessous de cinq
dossiers communs, rien n'est affiche : le kappa y passe de 0 a 1 par accident.

### Ce qu'un candidat peut demander

`/candidatures/<id>/explication-candidat.pdf` produit un document destine a la
personne concernee, au titre des articles 15 et 22 du RGPD. Ce n'est pas le
dossier interne : **ni motif de decision, ni nom du recruteur, ni rang, ni
mention des autres candidatures** — ce sont soit des appreciations qui ne lui
sont pas opposables sous cette forme, soit des donnees concernant d'autres
personnes. Cinq tests verrouillent ces absences, dont un sur les metadonnees du
fichier : un nom se lit aussi dans les proprietes d'un PDF.

Le vocabulaire est reecrit pour son lecteur. Un ecart y devient « ce que
l'offre demandait et que le CV n'indiquait pas », avec la precision qu'une
competence absente n'est pas une competence que le candidat n'a pas.

### Ou couper le classement

Le moteur ordonne les candidatures ; il ne dit pas laquelle est la derniere a
recevoir. En pratique on coupe quand meme, au chiffre rond. `apps/evaluation/
threshold.py` remplace le chiffre rond par un balayage : pour chacun des 101
seuils possibles, on compte qui passe et surtout **qui est ecarte a tort**.

Le cout de l'erreur est asymetrique. Recevoir un candidat moyen coute une heure
d'entretien ; ecarter un bon candidat coute un recrutement, et le cout est
supporte par quelqu'un qui n'en saura jamais rien. Le seuil retenu maximise donc
un F-beta avec beta = 2, qui pese le rappel quatre fois plus que la precision.
Ce choix est un jugement, pas un resultat : il est en constante, et la page
**Seuil de tri** affiche la courbe entiere pour qu'un recruteur puisse trancher
autrement.

```
Sur 36 profils annotes, dont 22 juges a recevoir :

seuil  retenus  bons  a tort  manques  precision  rappel     F2
  50 %      28    22       6        0      0.786   1.000  0.948
  70 %      24    22       2        0      0.917   1.000  0.982
  85 %      22    22       0        0      1.000   1.000  1.000   <- retenu
  90 %      17    17       0        5      1.000   0.773  0.809
 100 %       5     5       0       17      1.000   0.227  0.269
```

**Le resultat parfait est le point a ne pas croire.** A 85 %, le seuil separe le
jeu annote sans une erreur — mais sur une marge d'**un seul point** (85–86 %).
Une separation parfaite sur une marge aussi etroite en dit autant sur la
facilite du jeu que sur la qualite du moteur. La marge est donc affichee a cote
du seuil, et le seuil recommande est le **milieu** de l'intervalle optimal, pas
une de ses bornes : au bord haut, un point de score perdu fait perdre un bon
profil.

Le classement marque cette ligne, il ne l'applique pas : tout ce qui se trouve
dessous reste consultable et recevable.

### Ce qui manque a un candidat

`apps/matching/counterfactual.py` repond a la question qui suit le score. On
modifie une caracteristique du profil sur une copie en memoire, on rejoue le
moteur, on lit la difference — aucun modele de langage, le moteur coute 3 ms.

Le critere de minimalite est le **nombre** de changements, pas leur cout :
mettre « deux ans d'experience » et « un palier de CECRL » sur une meme echelle
d'effort supposerait une equivalence que rien ne justifie.

```
Profil actuel                                                      9 %
1. Acquerir « Django »       2 ans, obligatoire   +65,1 pts       74 %
2. Acquerir « PostgreSQL »   1 an, obligatoire     +8,9 pts       83 %
```

Deux details qui comptent. Les apports affiches sont **marginaux** : PostgreSQL
vaut 21,1 points pris seul, 8,9 une fois Django acquis, parce que le facteur de
recevabilite est multiplicatif. Afficher l'apport isole donnait un tableau dont
les lignes ne s'additionnaient pas. Et « Python » disparait du chemin apres
Django : l'ontologie dirigee le credite deja a 0,85.

**La localisation n'est jamais un levier.** « Demenagez » n'est pas un conseil
qu'un outil de recrutement a a donner, et c'est le critere que l'audit de biais
a identifie comme porteur d'un signal identitaire. Consequence assumee :
certains ecarts sont annonces hors de portee plutot que combles sur le papier.

### L'offre ou ce candidat passerait

Un candidat qui n'atteint pas le seuil sur l'offre a laquelle il a postule
disparait. C'est le comportement de tous les ATS, et c'est une perte seche pour
les deux parties. `apps/matching/redirect.py` regarde les autres offres
ouvertes avant de laisser tomber un dossier — c'est la consequence concrete de
« l'outil classe, il n'ecarte personne ».

**C'est un signalement, pas un transfert.** Aucune candidature n'est creee :
postuler ailleurs appartient au candidat, et proposer son dossier a une autre
equipe sans le lui demander poserait un probleme de finalite. La page distingue
« aucune autre offre ne conviendrait » de « on n'a pas regarde » : elle dit
combien d'offres ont ete examinees.

### Coherence du parcours, et ce qu'on refuse d'y mettre

Chevauchements d'emplois, dates inversees ou futures, diplome posterieur a
l'experience, anciennete declaree superieure aux periodes citees. Chaque
signalement est une regle calendaire qu'on peut rejouer a la main, et **aucun
ne touche au score**.

**Le module ne cherche pas a deviner si un CV a ete redige par un modele.** Les
detecteurs de texte genere affichent 10 a 30 % de faux positifs et
sur-signalent les locuteurs non natifs : un anglais scolaire correct ressemble
statistiquement a du texte genere. Dans un outil de recrutement, cela produit
exactement la discrimination que l'audit de biais passe son temps a traquer. Un
test verrouille ce refus : aucun code de signalement ne peut porter sur le
style d'ecriture.

La retenue est testee autant que la detection. Un preavis d'un mois n'est pas
un chevauchement, une reprise d'etudes n'est pas suspecte, et une interruption
de carriere est signalee en information avec la mention qu'elle ne doit pas
peser sur la decision. Un CV sans dates est declare **non verifiable** plutot
que « sans incoherence » : les deux phrases n'ont pas le meme sens, et la
seconde serait un mensonge.

### Dossiers en double

Un candidat qui repostule six mois plus tard, avec un CV remanie et son nom
saisi dans l'autre sens, cree aujourd'hui deux dossiers : deux scores, deux
historiques, et un recruteur qui peut ecarter le premier sans savoir que le
second existe.

Le rapprochement se fait par blocage — adresse, nom normalise, telephone — puis
par somme de signaux ponderes. **Le nom seul ne suffit jamais** : deux homonymes
sont deux personnes, et une fusion est irreversible. Rien n'est fusionne sans
qu'un recruteur habilite ne le valide, et chaque fusion est journalisee.

La fusion ne perd rien : une competence presente des deux cotes garde
l'anciennete la plus elevee, une candidature en double sur une meme offre
conserve celle qui est allee le plus loin dans le processus, et l'echeance de
conservation retenue est la plus tardive.

### Chercher dans le texte des profils

L'assistant traduit une question en criteres, et c'est ce qu'il faut quand la
question en contient. Mais « qui a travaille sur des systemes de paiement ? » ne
se traduit en aucun filtre : ce n'est ni une competence declaree, ni une langue,
ni un seuil. `apps/assistant/textsearch.py` cherche alors dans le texte.

**BM25 plutot qu'un `LIKE`.** Une recherche par sous-chaine classe au hasard :
un profil qui mentionne « paiement » vingt fois et un qui l'evoque une fois
sortent a egalite, et « SEPA » ne pese pas plus que « projet ». BM25 corrige les
deux — frequence saturante, rarete du terme, normalisation par la longueur du
document. Trois tests couvrent exactement ces trois proprietes.

**Fusion par rang.** Quand la couche vectorielle est disponible, les deux listes
sont fusionnees par Reciprocal Rank Fusion. Additionner un score BM25 — non
borne, dependant du corpus — et un cosinus dans [0, 1] supposerait une echelle
commune qui n'existe pas. Les rangs, eux, se comparent. Sans embeddings,
l'hybride se ramene au lexical, et l'interface le dit plutot que de laisser
croire a une recherche semantique.

Un jeu d'evaluation dedie, `search_v1`, fixe la pertinence **par
construction** : chaque profil est ecrit pour repondre ou non a des requetes
precises, arretees avant la premiere execution.

```
python manage.py evaluate_search --detail

  Rappel@5                          0,959
    plafond atteignable             0,959
  MRR                               1,000
  Precision@3                       0,929
  Requetes sans reponse traitees    1,000
```

**Le plafond n'est pas un detail de presentation.** La requete « Python » compte
sept profils pertinents pour cinq places : son rappel@5 ne *peut pas* depasser
0,71. Sans cette borne publiee, un sans-faute se lirait comme un manque. Ici
rappel@5 egale son plafond : aucun profil pertinent n'est rate.

Deux details du decoupage, trouves en ecrivant les tests. Un terme compose est
indexe entier **et** en morceaux : entier seul, « bout en bout » ne trouverait
pas « bout-en-bout » ; en morceaux seuls, « 3-D » disparaitrait, ses deux
moities tombant sous la longueur minimale. Et la requete sans reponse renvoie
une liste vide — la CI verifie ce comportement, parce qu'un moteur qui remplit
l'ecran a tout prix est pire qu'un moteur qui admet ne rien avoir.

### Rapport d'evaluation en PDF

Les chiffres vivent dans des pages web et des commandes. Un responsable
conformite, lui, demande un document date, versionne et transmissible.
`/transparence/rapport.pdf` le produit : qualite du classement, effet mesure des
attributs identitaires, seuil de coupe et sa marge, qualite de la recherche,
provenance.

**Aucune dependance ajoutee.** PyMuPDF est deja la, pour lire les CV ; il sait
aussi ecrire des PDF, tables, accents et metadonnees compris. ReportLab ou
WeasyPrint auraient alourdi l'installation — WeasyPrint reclame GTK sous
Windows — sans rien apporter de plus.

Le document porte quatre informations sans lesquelles il ne prouverait rien six
mois plus tard : version du moteur, version de chaque jeu d'evaluation, date, et
compte a l'origine de l'export. L'export est lui-meme journalise : un document
qui sort du systeme est une donnee qui circule.

**Deux rapports, deux publics.** Celui-ci dit ce que vaut le systeme.
`/candidatures/<id>/dossier.pdf` dit ce qui a ete fait d'un candidat : score
detaille par critere, ecarts, decisions avec leur motif et leur auteur,
questions d'entretien. C'est le document qu'un candidat peut demander au titre
de l'article 15 du RGPD, et celui qu'un recruteur emporte en entretien. **En
screening a l'aveugle, l'identite y reste masquee, metadonnees du fichier
comprises** — sans quoi l'export serait la porte de sortie que l'attenuation du
biais cherche a fermer.

Deux pieges rencontres. `insert_htmlbox` ne pagine pas — il rend ce qui tient et
signale le reste ; chaque bloc est donc mesure a blanc avant d'etre ecrit, faute
de quoi une section disparaitrait en silence, ce qui est la pire facon de perdre
un chiffre dans un document de conformite. Et le texte extrait d'un PDF contient
les **ligatures** de la police : « Effet » en ressort en « Eﬀet », si bien qu'une
verification par `in` echoue sur un document parfaitement correct.

### API REST

DRF etait installe, configure et affiche dans le schema d'architecture sans une
seule route. Il en a maintenant, sous `/api/` : offres, candidats, candidatures,
classement, ecarts contrefactuels, decision, recalcul.

Elle passe par les **memes services que l'interface** — `services.decide` valide
et journalise, le role est verifie et le refus journalise, le screening a
l'aveugle du compte appelant s'applique aux reponses. Ce dernier point n'est pas
cosmetique : masquer le nom en renvoyant l'adresse e-mail et le profil LinkedIn
aurait fait de l'attenuation du biais une formalite contournable en une requete.
Les champs sont vides et un booleen `blind` dit pourquoi, pour qu'un
consommateur distingue « absent » de « retire ».

```
GET  /api/offres/<slug>/classement/   rang, score, ecarts, versions de moteur
GET  /api/candidats/recherche/?q=     BM25, rang, score, termes trouves
GET  /api/candidatures/<id>/ecarts/   chemin vers le seuil, plafond atteignable
POST /api/candidatures/<id>/decider/  motif obligatoire pour ecarter
```

### Un agent qui prepare, et qui ne decide pas

Un CV depose declenche un agent : il calcule le score, redige l'analyse, propose
une suite au dossier, et prepare les questions d'entretien. Ce qu'il ne fait
pas, c'est faire avancer une candidature. **Il produit une recommandation ; un
recruteur la suit ou ne la suit pas.**

Cette limite n'est pas une consigne dans un prompt, c'est une propriete du
compte. L'agent a son propre role, `agent`, et ce role est **absent de
`can_decide`** : s'il appelait `services.decide`, il serait refuse et le refus
serait journalise, comme pour n'importe quel compte sans le droit. Le compte est
cree sans mot de passe utilisable et inactif — on ne s'y connecte pas. C'est
l'article 14 de l'AI Act pris au serieux : la supervision humaine tient parce
que la machine n'a pas le bouton, pas parce qu'on lui a demande de ne pas
appuyer dessus.

Verifie sur les donnees de demonstration, apres une execution reelle contre le
serveur d'inference : deux recommandations produites, **zero candidature
avancee, zero decision au nom de l'agent**, et chaque entree du journal marquee
`agent=True` — un audit peut donc separer ce qu'a fait la machine de ce qu'a
fait un humain.

**L'ordre des etapes est une decision de cout.** La recommandation passe avant
les questions d'entretien, pour que celles-ci soient sautees sur un dossier
propose au rejet. Preparer un entretien pour quelqu'un qu'on propose de ne pas
recevoir depense des tokens pour un entretien qui n'aura pas lieu. Mesure sur
les deux memes dossiers, avec les memes scores et les memes recommandations en
sortie :

| | etapes | tokens | duree |
|---|---|---|---|
| questions avant la recommandation | 8 | 5 050 | 28 648 ms |
| questions apres | 6 | **2 287** | **11 166 ms** |

Si un recruteur passe outre et fait avancer le dossier, les questions sont
generees au passage suivant.

**Trois garde-fous, parce qu'un agent qui tourne seul est un agent qui derape.**
Un interrupteur coupe par defaut (`AGENT_ENABLED`) — sans lui, l'agent ne
touche pas au modele. Un plafond de tokens glissant sur 24 h, compte sur les
appels reellement passes et non estime, qui arrete l'agent au dossier suivant
plutot qu'au milieu d'un dossier. Et un echec d'inference sur une etape ne fait
pas tomber l'execution : l'etape est comptee en echec, le dossier continue,
l'etape sera reprise au passage suivant puisque chaque etape sait dire si elle
est deja faite.

```
python manage.py run_agent --dry-run   # ce qu'il ferait, sans appeler le modele
python manage.py run_agent --limit 10
```

Sans broker Celery, le declenchement au depot d'un CV ne fait rien plutot que de
bloquer la requete d'upload le temps de deux appels au modele ; l'agent tourne
alors en lot par la commande. Avec un broker, le meme code part en asynchrone.

Ce qu'il n'est pas : il n'apprend pas, il ne reecrit pas les ponderations, il ne
change pas le seuil. Un agent qui ajusterait lui-meme ses propres criteres
rendrait injustifiable chaque decision passee — le moteur resterait explicable,
mais plus reproductible.

### La supervision est-elle reelle, ou seulement prevue ?

Que l'agent ne puisse pas decider se demontre en lisant le code. Que la
supervision soit **effective** ne se demontre pas du tout : ca se mesure. Un
recruteur qui suit toutes les propositions sans jamais en contredire une rend
la garantie structurelle purement formelle — la decision lui est imputee, elle
est prise par la machine.

Le **taux de contradiction** est la part des propositions qu'un humain a
ecartees. Sur le jeu de demonstration :

| | tranchees | contredites | intervalle a 95 % |
|---|---|---|---|
| Toutes propositions | 26 | 27 % | 14 – 46 % |
| Rejets proposes | 13 | **38 %** | 18 – 64 % |
| Mises en entretien proposees | 13 | **15 %** | 4 – 42 % |

**C'est la ventilation qui porte le resultat, pas le total.** Contredire les
rejets deux fois plus souvent que les mises en entretien decrit une supervision
qui se relache exactement la ou elle engage le moins : ecarter un candidat se
discute, le faire avancer se signe sans relire.

Aucune valeur n'est presentee comme la bonne. Ni 0 % ni 100 % ne sont
defendables — le premier decrit un tampon, le second un agent inutile — et
entre les deux cela releve du metier.

**L'intervalle est affiche avec le chiffre, pas en note de bas de page.** Une
contradiction sur quatre decisions donne 25 %, intervalle [5 %, 70 %] :
compatible avec a peu pres tout, tampon compris. Il est calcule par la methode de
Wilson et non par l'approximation normale, qui sur ces effectifs sort des
bornes negatives — un intervalle affiche a −12 % se voit et decredibilise le
reste de la page.

L'alerte « agent jamais contredit » se declenche sur la **borne haute**, jamais
sur le taux. Sans aucune contradiction cette borne vaut `z²/(n+z²)`, donc il
faut **35 decisions** pour qu'elle passe sous 10 %. C'est le prix pour qu'une
alerte veuille dire quelque chose : trois suivis sur trois ne prouvent rien.

Le graphique n'est volontairement pas une jauge. Une jauge dit qu'aller vers la
droite est bon ; ici les deux extremites sont mauvaises. L'intervalle y est
trace plus large que le point, parce que c'est lui l'information quand
l'effectif est petit.

### Une veille qui survit a ce qu'elle surveille

`monitoring.py` sait recalculer les ratios d'impact, les dater et alerter. Ce
qui lui manquait, c'est quelqu'un pour l'appeler : un controle qui n'existe que
sur une page qu'un responsable doit penser a ouvrir ne se declenche jamais
entre deux audits — precisement la periode ou une ponderation nouvelle peut
reintroduire un signal identitaire.

```
python manage.py agent_watch
python manage.py agent_watch --strict   # sort en erreur s'il y a une alerte
```

Trois proprietes, et les deux premieres sont le seul interet de la tache :

- **Elle ne coute aucun token.** Le ratio d'impact se calcule par le moteur
  deterministe. Elle n'est donc pas soumise au plafond, et continue de tourner
  quand le budget est epuise.
- **Elle tourne meme quand l'agent est coupe.** `AGENT_ENABLED` protege la
  depense, pas la surveillance. Un garde-fou qui s'arrete en meme temps que ce
  qu'il surveille ne garde rien.
- **Elle ne bloque rien**, comme le module qu'elle appelle : elle constate,
  date et signale.

Releve du jour sur le jeu annote : `localisation` 0.809, les trois autres
dimensions a 1.000, aucune alerte. Le premier releve ne peut par construction
detecter aucune derive — c'est le second passage qui commence a servir.

### Quels dossiers sont restes a moitie

Les executions comptent les etapes en echec, ce qui repond a « combien ». Un
exploitant a besoin de « lesquels » : un compteur a 3 sans moyen de savoir
quels dossiers sont concernes ne se traite pas.

La liste ne montre que les dossiers **entames puis laisses a moitie**, pas ceux
qui attendent simplement leur tour. Ce sont eux qui trompent : ils ont un
score, ils s'affichent comme les autres, et il leur manque l'analyse sur
laquelle un recruteur croit s'appuyer.

Chaque ligne porte une cause probable, et la distinction est ce qui rend la
liste exploitable : s'il ne manque que des etapes appelant le modele, c'est un
serveur injoignable et la reprise suffira ; s'il manque le **score**, c'est un
defaut, puisque ce calcul est local et deterministe et n'avait aucune raison
d'echouer.

### Parler aux candidats — et mesurer quand on ne leur parle pas

E-mail, WhatsApp, SMS, appel : les echanges avec un candidat sont modelises,
consentis, journalises, et **suggeres par le modele sans jamais partir seuls**.

**Le consentement vient avant le message**, dans la conception comme dans
l'ordre du code. L'e-mail et l'appel sont presumes ouverts : le candidat a
donne ces coordonnees *pour cet usage* et attend une reponse. WhatsApp et le
SMS arrivent sur un telephone personnel, souvent hors des heures de travail, et
demandent un accord explicite. Un accord tranche dans les deux sens, et c'est
le second qui compte le plus : **un retrait ferme un canal meme presume** —
quelqu'un qui demande a ne plus etre appele doit etre entendu, meme si l'appel
etait justifie par sa candidature. Aucun enregistrement n'ecrase le precedent :
prouver qu'un accord existait au moment de l'envoi suppose de conserver
l'historique.

Le consentement se verifie **a l'envoi, pas a la redaction**. Bloquer la
redaction aurait cache le probleme au lieu de le poser : le brouillon existe,
le refus explique ce qui manque, et enregistrer l'accord debloque l'envoi sans
reecrire le texte.

**En cas d'egalite de date, le refus l'emporte.** L'horloge de Windows avance
par paliers d'environ 15 ms, et la cle primaire est un UUID : deux
enregistrements poses dans le meme tic ne se departagent pas. Trier sur la
seule date rendait le resultat aleatoire — un retrait enregistre juste apres un
accord pouvait ne pas prendre effet, et le systeme aurait ecrit a quelqu'un qui
venait de demander le contraire. Le defaut a ete revele par un tirage de la
suite de tests en ordre aleatoire, pas par une relecture.

**Le modele de langage n'ecrit pas le message**, il personnalise un gabarit
deja valide — meme parti que pour l'explication d'un score. La consequence
compte plus que le principe : le pire resultat possible est le texte
generique, jamais un courrier faux envoye a une personne reelle. Un serveur
injoignable, une reponse tronquee, une sortie qui part en dissertation : dans
les trois cas le recruteur garde un brouillon correct. Le modele ne voit jamais
le CV brut, seulement une liste courte de faits deja extraits. **En screening a
l'aveugle, rien ne fuit par ici non plus** : la formule d'appel reste neutre et
aucun element identifiant n'est transmis.

Les gabarits sont **versionnes comme les prompts**, et chaque message conserve
la version appliquee. Ils existent en deux longueurs : coller cinq paragraphes
d'e-mail dans un WhatsApp produit un message que personne ne lit.

Le refus, lui, **ne paraphrase aucun chiffre**. Un texte redige qui « explique »
un rejet en reformulant un score se trompe tot ou tard, et cette version-la
sera la seule que le candidat aura lue. L'explication detaillee existe deja,
produite par le moteur, avec ses chiffres exacts — la bonne conduite est d'y
renvoyer. Le refus ne s'envoie ni par SMS ni par WhatsApp : c'est le seul
message du lot qui merite d'etre lu au calme.

**Trois etats de canal, parce qu'ils appellent trois conduites.**

| Canal | Accord | Expediteur |
|---|---|---|
| E-mail | presume | **connecte** (couche courriel de Django) |
| WhatsApp | explicite | modelise, non connecte |
| SMS | explicite | modelise, non connecte |
| Appel | presume | hors logiciel, se consigne |

WhatsApp et le SMS demanderaient un compte WhatsApp Business avec des gabarits
valides par Meta, et un contrat operateur. Rien de tout cela n'existe ici.
**Ecrire un faux expediteur qui journalise « envoye » aurait donne une
demonstration plus flatteuse et un systeme qui ment** : le jour ou les
identifiants arrivent, personne ne saurait plus quels messages sont reellement
partis. Ce que le projet apporte pour ces canaux, c'est tout sauf le cable.
L'appel, lui, ne sera jamais connecte — le ranger avec WhatsApp laisserait
croire qu'il manque du code a ecrire.

Sur une demonstration publique, **tous** les canaux sont fermes, e-mail
compris : une instance en ligne qui expedie de vrais courriers a des adresses
saisies par des inconnus est un incident, pas une fonctionnalite.

#### Ce que le premier essai a produit : « Bonjour EL, »

Le nom du candidat etait « EL AMRANI Sara », et la formule d'appel prenait le
premier mot. Le message serait parti tel quel.

Le probleme est general : « EL AMRANI Sara » met le nom de famille devant,
« Sara El Amrani » le met derriere, et rien dans la chaine ne dit lequel on
lit. Beaucoup de systemes tranchent quand meme et se trompent sur une partie
de leurs candidats — toujours la meme.

`apps/outreach/salutation.py` **ne devine pas**. Trois signaux permettent de
conclure : une casse mixte, ou les capitales marquent le nom de famille ; un
nom d'un seul mot ; une casse uniforme dont le premier mot n'est pas une
particule (`el`, `ben`, `ait`, `ould`, `van`, `de`...). Hors de ces cas, la
fonction renvoie une chaine vide et le message commence par « Bonjour, ».

Un nom entierement en capitales sur plusieurs mots — « BADR SAHRAOUI » comme
« ALAOUI YOUSSEF » — ne porte **aucun** signal d'ordre : les deux s'ecrivent
pareil et se lisent a l'envers l'un de l'autre. Le module renonce. Se tromper
de prenom dans un courrier de recrutement est pire que de ne pas en mettre.

#### Verifier que ca part vraiment

```
python manage.py outreach_selftest --to moi@example.com
```

Fabrique les trois messages qui comptent — invitation a un entretien, reponse
positive, reponse negative — et les expedie. **Sans `EMAIL_HOST` dans le `.env`,
rien ne peut partir** : la commande ecrit alors des fichiers `.eml` complets,
ouvrables dans Gmail, Outlook ou Thunderbird. Le fichier contient le message tel
qu'il serait recu, versions texte et HTML et marque liee comprises ; tout est
eprouve sauf le saut SMTP.

Ecrire un `.eml` plutot qu'annoncer « envoye » sans serveur suit la meme regle
que le reste du module : on ne simule pas un envoi. La candidature d'essai est
supprimee a la fin — une adresse reelle n'a rien a faire dans le jeu de
demonstration une fois le controle passe.

Pour envoyer pour de vrai, il suffit de renseigner `EMAIL_HOST`, `EMAIL_PORT`,
`EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` et `DEFAULT_FROM_EMAIL`. Aucun code ne
change : `base.py` bascule seul sur SMTP.

```
python manage.py check_email
```

Diagnostique la configuration **sans rien envoyer**, et sans jamais afficher le
secret — quatre caracteres d'une cle sont quatre caracteres de moins a deviner.
Chaque cause a son message et sa correction.

La distinction qui compte : **525 n'est pas 535**. Le premier dit « le serveur
a reconnu vos identifiants et refuse le compte », le second « vos identifiants
sont faux ». Les confondre fait regenerer en boucle une cle qui etait bonne. Le
cas s'est presente sur ce projet, sur un compte Brevo neuf en attente de
validation.

Et un piege trouve en ecrivant la commande elle-meme : sans identifiants,
Django n'appelle pas `login()`, la connexion s'ouvre, et l'outil annoncait
« prete » alors que rien n'avait ete verifie. **Un controle qui reussit a vide
est pire qu'absent** — il refuse desormais.

**Un defaut trouve en relisant les en-tetes produits.** L'objet de la reponse
positive arrivait precede d'une espace. Cause : un objet contenant un seul
caractere hors ASCII est encode selon la RFC 2047, et s'il est un peu long il
est replie sur deux lignes — `Subject:` reste vide et certains clients affichent
l'espace. Mesure : un objet ASCII de 84 caracteres ne se replie pas, un objet
non-ASCII de 61 caracteres se replie. Le tiret cadratin des objets est devenu
deux-points, et un test refuse desormais tout objet non-ASCII.

#### Le silence

La plainte la plus repandue sur le recrutement n'est pas le refus, c'est
l'absence de reponse. Outiller l'envoi de messages sans mesurer ceux qu'on
n'envoie pas outillerait surtout le confort du recruteur.

```
python manage.py outreach_report --strict   # sort en erreur s'il reste un oubli
```

Deux silences distincts, et les confondre perdrait le plus grave. **Apres une
decision** : le dossier est ecarte, le motif est ecrit, et personne n'a prevenu
l'interesse — l'information existe et n'est pas transmise. **Avant toute
decision** : un dossier ouvert depuis plus de 21 jours sans un seul message, ou
le candidat ignore meme que sa candidature est arrivee. Le seuil vient du delai
que les gabarits annoncent eux-memes : promettre une reponse sous quinze jours
et se taire vingt-et-un jours est un manquement a sa propre promesse.

Un appel consigne compte comme une reponse au meme titre qu'un e-mail expedie —
la question est « cette personne a-t-elle eu une reponse », pas « le logiciel
a-t-il expedie quelque chose ». Un message envoye **avant** la decision ne
compte pas : un accuse de reception ne previent pas d'un rejet decide ensuite.

Releve sur le jeu de demonstration : 1 candidat ecarte sur 2 jamais prevenu
(50 %), delai median de notification 4,9 jours. La liste nomme les dossiers,
du plus ancien au plus recent — un taux sans la liste ne se traite pas.

Un message envoye ne se modifie plus : corriger apres coup le texte d'un
courrier qu'une personne a deja lu transformerait le journal en fiction.

### Une marque, trois sorties

L'ecran, le PDF et le courriel puisent au **meme fichier** — `static/img/mark.svg` —
via `apps/core/brand.py`. Une identite recopiee a la main dans chaque gabarit
derive : l'ecran finit par dire une chose, le PDF une autre.

**Le concept vient d'un logo fourni** : deux documents, un CV et une offre,
relies. Deux choses ont change, et les deux viennent de l'avoir regarde aux
tailles reelles plutot qu'a 1400 px.

Les mots « CV » et « Offre » ont disparu. Lisibles en grand, ils formaient une
bouillie a 16 px — la taille a laquelle une favicon passe le plus clair de son
temps. Et les deux pages, d'abord posees cote a cote avec un connecteur entre
elles, se lisaient comme des **crochets** : le connecteur mangeait leurs bords
interieurs. En les faisant se recouvrir, la marque devient une silhouette
franche a 16 px — et dit quelque chose de juste, puisque l'intersection entre un
CV et une offre est exactement ce que le moteur calcule.

L'encre suit `currentColor` : un seul fichier sert sur fond clair et sur la
barre laterale sombre, sans variante a garder d'accord. Le SVG porte sa propre
regle `prefers-color-scheme`, pour rester lisible sur un onglet sombre.

**Les PNG ne sont pas stockes, ils sont rendus** depuis le SVG a la demande et
gardes en memoire. Ranger des PNG a cote du SVG aurait cree autant d'occasions
de les laisser diverger.

#### Ce que le courriel impose

Trois contraintes, et elles expliquent toute la mise en page :

- **Gmail et Outlook retirent les SVG.** La marque part en PNG.
- **Les images distantes sont bloquees par defaut**, et trahissent l'ouverture
  du message. La marque voyage donc en piece jointe liee (`cid:`), sans aucune
  requete sortante — verifie par un test qui refuse tout `http://` dans le HTML.
- **Les blocs `<style>` sont retires**, et le rendu d'Outlook n'honore que les
  tableaux. D'ou les styles en ligne et la disposition en tableau, bornee a
  600 px.

Le texte reste la **version de reference** : c'est lui qui est enregistre, relu
et journalise. Le HTML n'en est qu'une presentation, construite mecaniquement a
partir du meme corps — un test verifie qu'aucun mot du texte ne manque au HTML.

Un defaut trouve en regardant le rendu : les gabarits sont replies autour de 75
caracteres, pour un courriel texte. Transformer chacun de ces retours en `<br>`
figeait la coupure, et le lecteur voyait « Votre profil a retenu notre
attention » puis, a la ligne, « pour le poste de Data Engineer », quelle que
soit la largeur de son ecran. Les paragraphes se recomposent maintenant — mais
la signature garde ses retours. Le depart se fait sur la longueur de la ligne
precedente, comme le `format=flowed` du courrier electronique : une ligne pleine
a ete repliee, une ligne courte a ete voulue courte.

#### Dans les PDF

Bandeau en tete de la premiere page — marque, nom, filet — et **marque en pied
de chaque page**, pas seulement de la premiere : une page de rapport se
photocopie, se transfere et s'imprime seule, et doit dire d'ou elle vient sans
le reste du document. Les trois documents en beneficient : rapport d'evaluation,
dossier de candidature, explication destinee au candidat.

### Rejouer les decisions passees

Le projet affirme partout que le score est **deterministe et reproductible**.
Cette page cesse de l'affirmer : elle reprend les dossiers reellement tranches,
les recalcule avec le moteur d'aujourd'hui, et compare.

```
python manage.py replay_decisions --strict   # echoue si un score a bouge
                                             # a version de moteur egale
```

C'est le seul controle du projet qui eprouve la reproductibilite sur des
**decisions reelles** plutot que sur un jeu annote.

**La difficulte n'est pas de recalculer, elle est d'attribuer l'ecart.** Un
score qui change six mois plus tard peut venir de deux causes sans rapport : le
moteur a change de version — ce qu'on mesure — ou les donnees ont change, CV
re-extrait, competence corrigee, ponderation revue. Dans ce second cas le
moteur est innocent et le rejeu ne prouve rien. Chaque dossier porte donc la
mention `concluant`, et le rapport ne compte comme divergence que ce qui l'est.

Trois decisions de conception, toutes destinees a ne pas mesurer autre chose
que ce qu'on annonce :

- **On compare moteur a moteur.** Si un recruteur avait corrige le score a la
  main, confronter ce chiffre humain au chiffre recalcule ferait apparaitre
  tout dossier corrige comme une divergence du moteur.
- **On retient le dernier score calcule AVANT la decision**, pas le plus
  recent : rejouer contre un score posterieur comparerait le moteur a lui-meme.
- **Une divergence a version egale est un defaut, entre deux versions une
  evolution.** `reproductible` ne porte que sur la premiere — c'est
  l'affirmation exacte que le projet fait.

Ce qui interesse un auditeur n'est d'ailleurs pas l'ecart de score mais son
effet : **la decision aurait-elle bascule ?** Un dossier qui passe de 0,91 a
0,90 n'a rien change ; un dossier qui passe de 0,86 a 0,84 sous un seuil a 0,85
a tout change.

Releve sur le jeu de demonstration : 2 decisions rejouables, 1 identique au
chiffre pres, 1 ecart de 4 points imputable au passage du moteur 1.1.0 a 1.2.0,
qui aurait fait basculer le dossier. **Aucun ecart a version egale.**

La tolerance est de 0,05 point : le moteur additionne des flottants, et deux
executions peuvent differer sur le dernier bit sans que rien n'ait change. Elle
reste tres en dessous de ce qui ferait basculer une decision, et un test le
verifie.

### Le journal d'audit, enfin consultable

Le modele existait depuis l'origine — immuable, complet, alimente par chaque
action — et **aucune page ne l'affichait**. Pour un systeme classe a haut
risque, « montrez-moi tout ce qui est arrive a ce candidat » est la premiere
demande d'un auditeur comme d'un candidat exercant son droit d'acces. Un
journal qu'on ne peut pas lire ne prouve rien.

Filtrable par action, par auteur, par objet, par texte — et par **origine** :
machine ou humain. C'est la distinction qu'un auditeur cherche a etablir en
premier, et celle sur laquelle repose l'exigence de supervision humaine de
l'AI Act. Cliquer sur le type d'un objet ramene tout ce qui lui est arrive :
depot, extraction, scores, consultations, decisions, messages, purge.

Un piege trouve en testant ce filtre : `exclude(metadata__agent=True)` ne rend
pas « les entrees humaines ». Sur une entree ou la cle est absente, la
comparaison vaut `NULL`, sa negation vaut `NULL`, et la ligne disparait — le
filtre « humain seul » ne renvoyait **rien**, c'est-a-dire l'inverse de ce
qu'il annonce.

### Ce que le modele fait varier, et ce qu'il ne touche jamais

L'argument central du projet tient en une phrase : le modele de langage
n'attribue aucune note, il commente un chiffre deja calcule. Tant qu'elle reste
une phrase, elle vaut ce que vaut une phrase.

```
python manage.py measure_variance --tirages 3
```

Trois analyses du **meme** score, contre le serveur d'inference reel :

| | Resultat |
|---|---|
| Score | **0,8535 sur les trois tirages** |
| Vocabulaire commun entre deux tirages | 0,404 — **60 % des mots changent** |
| Longueurs | 290, 387, 361 mots |
| Amplitude | 97 mots |

Le score ne peut pas bouger : il n'est pas recalcule, il est passe en entree au
modele, qui ne peut que le mettre en mots. C'est une propriete **structurelle**,
pas statistique — la mesure ne la decouvre pas, elle la donne a voir.

La mesure qui apprend vraiment quelque chose est ailleurs : **le modele
invente-t-il des chiffres ?** Une analyse qui ecrirait « 72 % sur les
competences » quand le moteur a calcule 68 % donnerait au recruteur un chiffre
faux avec l'autorite d'un chiffre calcule. Tous les pourcentages du texte sont
donc releves et confrontes au detail du score.

**Et voici le resultat honnete : sur les trois tirages, le modele n'a cite aucun
pourcentage.** Le controle passe donc sans avoir ete eprouve — un controle qui
ne se declenche jamais ne prouve rien. Ce sont les tests unitaires, sur des
textes fabriques pour l'occasion, qui verifient qu'il attrape bien un chiffre
absent du score.

Le recouvrement de vocabulaire n'est pas une mesure de sens : deux textes
peuvent dire la meme chose avec d'autres mots, et la mesure les dira differents.
Il repond a « le modele repete-t-il sa copie ou reformule-t-il », pas a « le
texte est-il bon ».

La page ne mesure **jamais au chargement**, seulement sur un bouton : chaque
tirage appelle le modele. Une page qui mesurerait a chaque visite serait une
facture qui court toute seule.

### CV distincts au contenu commun

A ne pas confondre avec « Doublons », qui cherche une meme personne sous deux
dossiers. Ici les candidats sont **differents** et le texte se ressemble : un CV
recopie, un modele partage dans une promotion, une agence qui reformate le meme
profil pour deux clients.

```
python manage.py check_plagiarism
```

Le fichier strictement identique est deja traite ailleurs — l'empreinte du
contenu est unique. Reste le cas difficile : deux fichiers differents dont le
texte se recouvre.

**Le tout-venant fausse tout.** « Experience professionnelle », « Langues :
francais, anglais », « Permis B » se retrouvent dans un CV sur deux ; une mesure
naive rapproche tout le monde de tout le monde. Deux garde-fous :

- des empreintes de **huit mots consecutifs**, qui ne se retrouvent identiques
  que si deux textes partagent une phrase entiere — un fait, pas un hasard ;
- le **retrait des empreintes presentes dans plus de 30 % du corpus**, soit
  l'equivalent, au niveau de la phrase, de ce qu'un mot vide est au niveau du
  mot.

Le seuil de signalement est volontairement haut : ici un faux positif porte une
accusation, un faux negatif ne fait rien perdre.

**Le module n'accuse personne.** Un fort recouvrement peut venir d'une copie
comme d'un modele d'ecole partage entre camarades de promotion. Il produit une
liste a regarder ; un humain tranche.

La comparaison est quadratique : instantanee sous quelques milliers de CV, elle
demanderait un pre-filtrage par empreintes minimales au-dela. La limite est
connue et n'est pas franchie ici.

**Une fuite trouvee en ecrivant cette page.** Le screening a l'aveugle masquait
les noms, et la page affichait juste en dessous `Alice Martin.pdf` — un CV
s'appelle presque toujours du nom de son auteur. La page « CV deposes » faisait
pire : elle affichait le nom **et** le fichier sans tenir aucun compte du mode
aveugle. L'attenuation du biais etait annulee par une liste de depots, page en
apparence anodine.

### Mettre la demonstration en ligne

`render.yaml` decrit le service entier : web Python, base PostgreSQL geree,
variables d'environnement, sonde de sante. Le deploiement se fait en trois
etapes, sans conteneur a construire.

1. Sur Render, **New → Blueprint**, puis pointer sur ce depot.
2. Render lit `render.yaml`, cree le service et la base, genere `SECRET_KEY`.
3. Ajuster `ALLOWED_HOSTS` et `CSRF_TRUSTED_ORIGINS` si le nom du service
   differe de `recrutement-ia`.

`scripts/build.sh` installe, collecte les statiques, migre, seme le jeu de
demonstration et calcule les scores. **Chaque etape qui echoue arrete le
deploiement** : mieux vaut ne pas deployer qu'exposer une version a moitie
migree.

**Ce que la demonstration ne montrera pas, et pourquoi.** Les deux modeles
tournent sur un serveur d'inference prive, injoignable depuis l'exterieur.
L'analyse redigee et les questions d'entretien y sont donc indisponibles. Tout
le reste fonctionne, parce que rien d'autre n'a jamais eu besoin d'un modele :
score, classement, ecarts, seuil de tri, recherche, doublons, exports. Un
bandeau le dit sur chaque page plutot que de laisser un visiteur conclure a une
panne. Le plan gratuit met le service en veille apres quinze minutes sans
trafic ; le premier chargement suivant prend une trentaine de secondes.

**Compte de visite.** Se connecter avec `observateur` : lecture seule sur tout,
refus journalise sur toute action. Un visiteur voit l'application entiere sans
pouvoir abimer le jeu de demonstration — le controle de role existait deja, il
sert ici de bac a sable.

Ce que la configuration promet est verifie hors ligne, dans
`tests/test_deployment.py` : la sonde repond sans session et tombe a 503 si la
base est injoignable, le manifeste declare la sonde et ne contient aucun
secret, le script de construction s'arrete a la premiere erreur, et les
reglages de production sont stricts. Verification supplementaire faite a la
main sur le point d'entree WSGI — celui que gunicorn charge :

```
ok  /sante/                200      sonde : {"status": "ok", "moteur": "1.2.0"}
ok  /comptes/connexion/    200      feuille /static/css/app.314c086cceb7.css
ok  /                      302      non authentifie -> connexion
ok  /api/                  403      refus explicite, pas une redirection
    WhiteNoise             200, 20 431 octets, cache immutable
    X-Frame-Options DENY · X-Content-Type-Options nosniff · Referrer-Policy same-origin
```

### Tests

```powershell
pytest              # suite complete
pytest -m "not llm" # sans les tests necessitant le serveur d'inference
ruff check .
```

---

## Configuration

Tout passe par le `.env` (voir `.env.example`).

| Variable | Role |
|---|---|
| `DATABASE_URL` | SQLite par defaut ; PostgreSQL en production |
| `CELERY_BROKER_URL` | vide = execution synchrone (pas besoin de Redis en dev) |
| `LLM_BASE_URL` / `LLM_MODEL` | modele texte, API compatible OpenAI |
| `VLM_BASE_URL` / `VLM_MODEL` | modele vision pour les CV a mise en page complexe |
| `EMBEDDING_PROVIDER` | `local` (fastembed ONNX) ou `server` (`/v1/embeddings`) |
| `AGENT_ENABLED` | interrupteur de l'agent d'orchestration, coupe par defaut |
| `AGENT_DAILY_TOKEN_BUDGET` | plafond glissant sur 24 h, mesure sur les appels passes |
| `OUTREACH_ORGANISATION` | nom qui signe les messages aux candidats |
| `OUTREACH_RESPONSE_DAYS` | delai de reponse annonce — et mesure ensuite |
| `DATA_RETENTION_DAYS` | duree de conservation des donnees candidat (RGPD) |
| `BLIND_SCREENING_DEFAULT` | masquage des attributs identitaires par defaut |

Le projet tourne **sans Docker, sans PostgreSQL et sans Redis** en
developpement. Ces composants ne deviennent necessaires qu'en production.

---

## Organisation du code

```
config/            reglages (base / dev / prod), URLs, Celery
apps/
  core/            modeles de base, journal d'audit immuable, seed_demo
  accounts/        utilisateur RH, roles, preference de screening a l'aveugle
  ai/              client d'inference, embeddings, prompts versionnes, check_ai
  jobs/            offres, competences attendues, ponderation du score
  candidates/      candidats, CV, donnees extraites + preuves, candidatures
  parsing/         extraction PDF/DOCX, diagnostic de mise en page, ancrage
  matching/        ontologie de competences, moteur de score, classement
  evaluation/      metriques, jeux annotes, harnais de non-regression
  agent/           orchestration : prepare un dossier, propose, ne decide pas
  outreach/        echanges avec les candidats : consentement, gabarits, silence
templates/         interface — composants partages dans partials/
static/css/        systeme de design, sans dependance externe
tests/             suite pytest
```

### Points d'entree a lire en premier

| Fichier | Interet |
|---|---|
| `apps/ai/client.py` | appel modele avec sortie contrainte par JSON Schema, reprise sur erreur, pool de connexions, tracabilite |
| `apps/ai/mock_server.py` | banc d'essai compatible OpenAI : rend la couche reseau verifiable sans serveur |
| `apps/ai/prompts.py` | registre de prompts versionnes — un prompt ne se modifie pas, il s'incremente |
| `apps/ai/embeddings.py` | fournisseurs d'embeddings interchangeables, recherche vectorielle |
| `apps/candidates/models.py` | modele de preuve (`EvidenceSpan`) : aucune donnee sans citation |
| `apps/core/models.py` | journal d'audit immuable (AI Act) |
| `apps/parsing/quality.py` | detection de couloir vide : c'est ce qui aiguille vers le modele vision |
| `apps/parsing/evidence.py` | ancrage tolerant des citations, avec calcul des coordonnees |
| `apps/parsing/pipeline.py` | orchestration complete, fusion des periodes d'experience |
| `apps/matching/ontology.py` | relations entre competences — **dirigees** : Django implique Python, jamais l'inverse |
| `apps/matching/management/commands/probe_semantic.py` | la mesure qui a fait desactiver le rapprochement semantique |
| `apps/matching/engine.py` | calcul du score, renormalisation des poids, degradation controlee |
| `apps/matching/explain.py` | le LLM ne voit que le detail chiffre, jamais le CV brut |
| `apps/agent/pipeline.py` | les etapes de l'agent, et l'ordre qui evite de preparer un entretien qu'on propose de ne pas tenir |
| `apps/agent/budget.py` | plafond de tokens mesure sur les appels reels, pas estime |
| `apps/agent/adoption.py` | taux de contradiction et intervalle de Wilson : la mesure qui separe une supervision reelle d'un tampon |
| `apps/agent/watch.py` | veille sans token, qui tourne meme quand l'agent est coupe |
| `apps/outreach/silence.py` | ce qu'on n'a pas dit aux candidats — la mesure qui manque a la plupart des ATS |
| `apps/outreach/salutation.py` | par quel prenom appeler quelqu'un, ou renoncer plutot que de se tromper |
| `apps/outreach/backends.py` | quels canaux partent vraiment, et lesquels le disent au lieu de le simuler |
| `apps/core/brand.py` | la marque, source unique de l'ecran, du PDF et du courriel |
| `apps/evaluation/replay.py` | rejeu des decisions reelles — et l'attribution d'un ecart, qui est le vrai sujet |
| `apps/evaluation/variance.py` | le modele invente-t-il un chiffre ? la seule faute grave qu'il puisse commettre ici |
| `apps/candidates/plagiarism.py` | CV distincts au contenu commun, et le retrait du tout-venant qui rend la mesure lisible |
| `static/img/mark.svg` | le dessin, et pourquoi il ne porte plus de texte |
| `apps/evaluation/harness.py` | reconstruit les cas annotes, mesure, puis annule tout |
| `apps/evaluation/bias.py` | audit par contrefactuels, ratio d'impact, proprietes verifiees |
| `apps/evaluation/cv_factory.py` | genere des CV dont la verite terrain est connue par construction |
| `apps/evaluation/extraction.py` | compare le profil reconstitue au profil d'origine |
| `apps/evaluation/datasets/ranking_v1.json` | le jeu annote — le champ `tests` dit ce que chaque cas eprouve |
| `static/css/app.css` | systeme de design : jetons, composants, theme clair et sombre |
| `static/js/charts.js` | graphiques SVG sans bibliotheque, infobulles, acces clavier |
| `scripts/audit_charts.mjs` | audit de geometrie : echoue si une marque sort du cadre |

---

## Feuille de route

- [x] Fondation : configuration, modele de donnees, couche IA, interface, tests
- [x] Extraction des CV : PyMuPDF, python-docx, aiguillage vision, ancrage des preuves
- [x] Moteur de scoring deterministe + explication par le LLM
- [x] Classement et analyse des ecarts de competences
- [x] Harnais d'evaluation : jeu annote, nDCG@5, non-regression en CI
- [x] Interface : systeme de design sans dependance externe, theme clair et sombre
- [x] Comparaison cote a cote de plusieurs candidats
- [x] Tableau de bord de cout et de latence, graphiques sans dependance externe
- [x] Audit de biais par contrefactuels, ratio d'impact, page de transparence
- [x] Screening a l'aveugle agissant sur le score, avec mesure de son effet
- [x] Harnais d'evaluation de l'extraction, a verite terrain generee
- [x] Generation de questions d'entretien ancrees dans le CV
- [x] Recherche en langage naturel : traduction en filtres, execution en base
- [x] Decision humaine tracee, roles appliques, purge RGPD automatique
- [x] API REST : memes services, memes roles, meme screening a l'aveugle
- [x] Ecarts contrefactuels : ce qui manque pour atteindre le seuil, mesure
- [x] Rapprochement des dossiers en double, fusion validee par un recruteur
- [x] Seuil de shortlist calibre sur le jeu annote, avec sa marge
- [x] Recherche BM25, fusion par rang avec le vectoriel, jeu d'evaluation dedie
- [x] Rapport d'evaluation exportable en PDF, sans dependance ajoutee
- [x] Dossier de candidature exportable, identite masquee en screening a l'aveugle
- [x] Explication destinee au candidat (RGPD art. 15 et 22)
- [x] Redirection positive vers une autre offre ouverte
- [x] Controles de coherence du parcours, sans detection de texte genere
- [x] Simulateur de ponderation montrant l'effet sur le ratio d'impact
- [x] Surveillance continue du biais, avec historique et alerte de derive
- [x] Accord entre recruteurs (kappa de Cohen) et ecart au score
- [x] Traitement de l'arabe : normalisation, recherche, rapprochement de noms
- [x] Configuration de deploiement verifiee hors ligne (sonde, statiques, securite)
- [x] Agent d'orchestration : prepare un dossier, propose, sans le droit de decider
- [x] Taux de contradiction avec intervalle de Wilson : mesurer la supervision, pas la supposer
- [x] Veille de biais sans token, qui survit a la coupure de l'agent
- [x] Echanges avec les candidats : consentement par canal, gabarits versionnes, suggestion IA
- [x] Mesure du silence : les candidats ecartes que personne n'a prevenus
- [x] Identite visuelle unique : ecran, PDF et courriel rendus depuis un seul SVG
- [x] Rejeu des decisions passees : la reproductibilite verifiee, plus seulement affirmee
- [x] Journal d'audit consultable et filtrable, machine separee de l'humain
- [x] Variance du modele mesuree : le score ne bouge pas, la redaction si
- [x] CV distincts au contenu commun, tout-venant retire, sans accusation

---

## Limites assumees

- Le jeu d'evaluation obtient des scores tres eleves sur cinq de ses sept cas.
  Les deux cas difficiles (`arbitrage_competences_anciennete`,
  `profils_proches`) ont ete ajoutes pour cette raison, et l'un des deux met
  effectivement le moteur en defaut. Etoffer le jeu reste le principal levier
  d'amelioration.
- Le rapprochement des localisations est une comparaison textuelle. Un
  geocodage (distance reelle, temps de trajet) serait plus juste — et
  reduirait sans doute le ratio d'impact de 0.809 mesure sur ce critere.
- L'audit ne couvre que le moteur deterministe. L'extraction du CV et la
  redaction de l'analyse, toutes deux confiees a un modele de langage, ne sont
  pas auditees : les mesurer demanderait un serveur d'inference en CI.
- Les CV du jeu d'evaluation de l'extraction sont **generes**, donc plus
  propres que les vrais : ni abreviation exotique, ni mise en page fantaisiste,
  ni scan de travers. Les scores obtenus sont optimistes d'autant.
- L'extraction depuis une mise en page en cellules reste faible (F1 0.46 sur
  `frontend_tableau`). Le cas est conserve dans le jeu precisement pour que
  cette faiblesse reste visible.
- Le score de compatibilite est une **aide au tri**, pas une decision. Aucune
  candidature n'est ecartee automatiquement : toute sortie du processus est
  imputee a un utilisateur identifie et journalisee.
- Le systeme reproduit les biais presents dans les criteres qu'on lui donne.
  Le mode screening a l'aveugle et l'audit d'ecart de score servent a les
  mesurer, pas a les supprimer.
- L'agent d'orchestration deplace un risque plutot qu'il ne le supprime : il ne
  peut pas decider, mais une recommandation posee sur un dossier **oriente**
  celui qui la lit. Le taux de contradiction est la pour le mesurer — mais les
  chiffres affiches en demonstration proviennent d'un historique **genere**,
  pas d'un usage reel. La mesure est eprouvee, la valeur ne l'est pas : elle
  dira quelque chose sur un vrai service, pas ici.
- Deux canaux sur quatre ne sont pas connectes. WhatsApp et le SMS sont
  modelises, consentis, journalises et redigeables, sans fournisseur derriere.
  Le modele de donnees et l'interface d'expedition sont ecrits ; y brancher un
  fournisseur est l'affaire d'une classe. Tant que ce n'est pas fait, ces
  canaux ne sont pas une fonctionnalite livree.
- Les objets de courriel ecrits par le projet sont en ASCII, ce qui evite le
  repliement RFC 2047 et l'espace parasite qu'il laisse en tete du titre. Mais
  l'**intitule du poste** est injecte dedans, et ce module ne le choisit pas :
  une offre dont le nom porte un accent, dans un objet long, reproduira le
  defaut. Django refuse un en-tete pre-encode multi-lignes — son garde-fou
  contre l'injection — ce qui ferme la correction generale sans reecrire
  l'assemblage du message.
- Le taux de silence se calcule sur les messages que **ce** systeme connait. Un
  recruteur qui repond depuis sa boite personnelle sans rien consigner
  apparaitra comme silencieux. La mesure sous-estime donc les reponses et
  surestime le silence — c'est le sens d'erreur le moins dangereux des deux,
  mais c'en est un.
- La formule d'appel renonce a nommer les candidats dont le nom est
  entierement en capitales, ce qui est frequent en tete de CV. Ces personnes
  recoivent « Bonjour, » plutot que leur prenom. C'est un choix : l'alternative
  etait de tirer a pile ou face sur l'ordre du nom.
- Le taux de contradiction ne distingue pas un recruteur qui contredit apres
  avoir lu d'un recruteur qui contredit par principe. Le delai median de
  decision est affiche a cote, mais il est domine par le moment ou le
  recruteur se connecte, pas par le temps qu'il passe a lire : un delai long
  ne prouve pas l'attention. Seul un delai median de quelques secondes serait
  un signal exploitable.
