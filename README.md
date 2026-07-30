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
- [x] Configuration de deploiement verifiee hors ligne (sonde, statiques, securite)

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
