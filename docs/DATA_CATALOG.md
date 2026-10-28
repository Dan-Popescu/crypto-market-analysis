# Catalogue des données

Un marché de référence est choisi par actif et par plateforme. Les plateformes principalement cotées en stablecoins privilégient USDT, puis USDC ; Coinbase, Kraken, Bitstamp, Gemini, Bitfinex et Crypto.com privilégient USD. Cette convention n'est pas une mesure du marché le plus profond et ne couvre pas toutes les paires d'une plateforme. Les historiques et carnets de devises différentes ne sont jamais additionnés implicitement.

Toutes les dates de marché sont en UTC. Les observations portent source, actif, métrique, unité, cotation éventuelle, date de l'événement et date d'ingestion ; les versions et modes sont conservés dans SQLite. Une donnée absente n'est ni zéro ni un signal neutre. L'accès à une source peut échouer (notamment 403) ; consultez `status` et les erreurs. USD, EUR, USDT et USDC ne sont ni fusionnés ni convertis implicitement.

| Famille | Source et contenu | Interprétation et limites |
| --- | --- | --- |
| Prix et volumes | Plateformes du registre, bougies horaires OHLCV ; volumes cotés et achats agressifs lorsqu'ils sont publiés | Les journées n'utilisent que 24 heures alignées et closes. Les semaines complètes sont lundi–dimanche UTC. Pas d'archive de ticks ni de fusion de devises. |
| Carnets | Bid/ask, spread en points de base, profondeurs à 0,5 %, 1 % et 2 %, déséquilibre | Valeurs en devise native. Si le carnet ne couvre pas l'intégralité de la bande, la profondeur est une **borne inférieure**, explicitement signalée ; ne pas la comparer comme une profondeur complète. |
| Dérivés | Intérêt ouvert et financement publiés par les plateformes | Unités et taille de contrat sont propres à la source ; l'unité peut être inconnue. Les taux réalisés et projetés diffèrent ; la période de financement peut être inconnue et des taux à 1 h et 8 h ne sont pas directement comparables. |
| Options | Deribit : IV près du cours pour horizons cibles 7, 30 et 60 jours, intérêt ouvert par échéance et écart d'IV puts/calls | Échéance réelle la plus proche sous contraintes de liquidité et de distance ; elle ne correspond pas nécessairement exactement à l'horizon cible. L'écart rapproche des strikes à moneyness comparable, **pas** un skew 25-delta. IV cotée, pas probabilité calibrée. |
| Réseau | Coin Metrics Community : adresses actives, transactions, capitalisation réalisée lorsque disponibles | Vérifiez couverture et conditions de licence de la source ; la licence Community est non commerciale (NC). |
| Stablecoins | DefiLlama : offre agrégée des stablecoins indexés USD | Série globale multi-chaînes, pas flux de capitaux attribué à BTC ou ETH. |
| Macro | FRED : taux réels 10 ans, dollar pondéré, taux 2 et 10 ans, VIX | Les valeurs historiques proviennent du millésime courant ; leur disponibilité exacte à l'époque n'est pas connue. Elles ne sont pas utilisées comme variables historiques dans les scénarios. |
| Calendrier et ETF | Événements importés manuellement et sourcés | Aucun calendrier exhaustif ni flux ETF automatique garanti. Les événements ne prouvent pas la publication à une date historique ; l'import dédié de métriques ETF manuelles reste à confirmer dans l'interface. |

Les scénarios initiaux sont expérimentaux, fondés sur un historique horaire agrégé en journées complètes et des régimes historiques voisins. Ils distinguent clôture en baisse et franchissement de barrières ; ils ne sont ni une prévision causale ni une probabilité calibrée. Une évaluation chronologique insuffisante doit rester explicitement insuffisante.
