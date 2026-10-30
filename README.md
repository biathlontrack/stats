# Biathlon Datacenter

Site web pour suivre la Coupe du monde de biathlon depuis un téléphone ou un ordinateur. Il propose :

- les classements de la Coupe du monde (général et par discipline) des dix dernières saisons ;
- les nations et leurs athlètes ;
- le calendrier et les résultats de chaque course, avec le détail du tir ;
- une fiche par athlète : biographie, équipement, palmarès, classements saison par saison, grands championnats, statistiques de tir et de ski.

Les données proviennent du Datacenter de l'IBU ([biathlonresults.com](https://www.biathlonresults.com)). Ce site n'est pas officiel et n'a aucun lien avec l'IBU.

## Fonctionnement

L'API de l'IBU n'accepte pas les appels faits depuis le navigateur d'un autre site. Le site fonctionne donc en deux parties :

1. **Collecte** (`scripts/build_data.py`). Une tâche GitHub Actions interroge l'API de l'IBU et enregistre des fichiers JSON allégés dans `site/data/`. Elle tourne toutes les 30 minutes de novembre à mars, et une fois par jour le reste de l'année.
2. **Publication**. Le dossier `site/` est publié sur GitHub Pages. La page `site/index.html` lit les fichiers JSON ; elle n'a besoin d'aucun serveur.

Les données déjà collectées sont gardées dans le cache GitHub Actions. Les saisons terminées et les courses officielles ne sont jamais retéléchargées ; seule la saison en cours est rafraîchie.

Ce n'est pas du vrai direct : pendant une course, les résultats affichés peuvent avoir jusqu'à 30 minutes de retard. Le lien « Direct officiel » de chaque page de course mène au site de l'IBU.

## Mise en place

1. **Settings → Pages → Build and deployment → Source : GitHub Actions**.
2. **Actions → Données IBU et publication → Run workflow**. Ce n'est utile que si aucune exécution n'a encore eu lieu.

La première collecte prend une dizaine de minutes. Le site est ensuite disponible à l'adresse `https://<compte>.github.io/<dépôt>/`.

## Réglages

Ce sont des variables d'environnement, à définir dans le workflow :

| Variable | Défaut | Rôle |
|---|---|---|
| `SEASONS_BACK` | `10` | Nombre de saisons passées archivées |
| `IBU_WORKERS` | `4` | Requêtes simultanées vers l'IBU |
| `MAX_ATHLETES` | `0` (illimité) | Plafond de fiches athlètes téléchargées par passage |

## En local

```sh
python3 scripts/build_data.py --out site/data
python3 -m http.server -d site 8000
```

Puis ouvrir http://localhost:8000.
