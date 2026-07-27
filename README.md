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
que ce fichier.

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

Interface sur http://127.0.0.1:4040/ — compte de demonstration
`recruteur` / `demo-recrutement-2026`.

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
- [ ] Comparaison cote a cote de plusieurs candidats
- [x] Tableau de bord de cout et de latence, graphiques sans dependance externe
- [x] Audit de biais par contrefactuels, ratio d'impact, page de transparence
- [x] Screening a l'aveugle agissant sur le score, avec mesure de son effet
- [x] Harnais d'evaluation de l'extraction, a verite terrain generee
- [x] Generation de questions d'entretien ancrees dans le CV
- [ ] Recherche hybride BM25 + vectorielle, recherche en langage naturel
- [ ] Generation de questions d'entretien ancrees dans le CV
- [ ] Rapport d'evaluation exportable en PDF
- [ ] Purge RGPD automatique, tableau de bord de cout et de latence

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
