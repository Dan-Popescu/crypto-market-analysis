# Import du prototype — 28 octobre 2026

## Origine et portée

Source : archive `crypto-observer-v0.1.0.zip` fournie par Dan.
Le nom du projet, du paquet Python et de la commande devient Crypto Market
Analysis, `crypto_market_analysis` et `crypto-market-analysis`.
Les références de documentation, tests, configuration MCP, Docker et verrou des
dépendances suivent ce renommage. Les noms de configuration `OBSERVER_*`,
`observer.toml`, `observer.sqlite` et le scope `observer:read` restent identiques.

Cette passe prépare le dépôt et son développement local ou dans le cloud.
Les calculs de marché et les prévisions du prototype restent inchangés.
Les fonctionnalités expérimentales existantes conservent leurs limites.

## Vérifications de l'import

Vérifications exécutées le 28 octobre 2026 sur Windows avec Python 3.12.3,
dans un environnement isolé contenant les dépendances verrouillées :

- `uv run --frozen python tools/check.py` : réussite.
- `uv lock --check --offline` : verrou cohérent.
- Ruff sur `src`, `tests` et `tools` : aucune erreur.
- Suite complète : **71 tests réussis** en 20,79 secondes.
- Protocoles MCP local STDIO et HTTP, et retour d'image native : inclus dans les tests.
- `uv build --wheel --offline` : distribution renommée construite.

Le premier essai des tests MCP dans l'environnement restreint ne s'est pas
terminé ; il a été interrompu. La suite complète a ensuite réussi avec l'accès
nécessaire aux processus et connexions locales. Aucun changement du code MCP
n'a été nécessaire.

Aucune collecte publique, connexion de compte IA, intégration Ledgerly ou
installation Docker n'a été effectuée pendant cet import. Leur fonctionnement
réel n'est donc pas validé par cette passe.

## Contenu du dépôt

Le dépôt conserve le code, les tests, la documentation et les exemples
synthétiques. Les sept rapports et graphiques réels du ZIP, le wheel fourni,
les bases et les environnements Python sont exclus du versionnement.
Le compte rendu de validation de l'auteur reste séparé dans VALIDATION.md.

Le dépôt a été initialisé en privé. Le 28 octobre 2026, Dan a ensuite choisi
un dépôt public pour faciliter les essais de pipeline et les vérifications
sur GitHub lorsqu'elles seront utiles. GitHub Actions reste désactivé à cette
étape ; aucun workflow n'est ajouté par ce changement de visibilité.
Une commande commune de vérification est fournie pour le PC et Codex cloud ;
ses résultats doivent être consignés avant chaque fusion.

## Limite déjà observée

Le marqueur de fraîcheur de chaque mesure de liquidité utilise le rythme
général du marché, et peut présenter comme fraîche une mesure vieille de
30 minutes alors que la collecte de liquidité est prévue toutes les cinq
minutes. Ce point a été reproduit pendant la lecture du prototype ; il reste
à corriger dans une tâche distincte. L'import ne modifie pas ce calcul.
