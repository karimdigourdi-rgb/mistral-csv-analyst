# mistral-csv-analyst

Deux livrables Mistral dans un même dépôt :

## 1. `agent/` — Analyste CSV conversationnel (Mistral Agents SDK)

Agent déployé sur Mistral Cloud (déploiement géré) qui :

1. reçoit un lien vers un CSV de votre Google Drive (ou le contenu collé) ;
2. le charge via une connexion Google Drive (connecteur `google_drive` configuré dans Mistral Studio) ;
3. répond aux questions en langage naturel avec **outils fixes uniquement** — aucune exécution de code arbitraire :
   - `charger_csv` : charge/lit le fichier (lien Drive partagé ou texte brut) ;
   - `decrire_colonnes` : décrit les colonnes (type, remplissage, plage, exemples) ;
   - `filtrer_lignes` : filtre les lignes (12 opérateurs : =, ≠, >, ≥, <, ≤, contient, commence par, dans, entre, vide, non vide) ;
   - `agreger` : agrégats group-by (compte, somme, moyenne, médiane, min, max, valeurs uniques) ;
4. mémorise le fichier et toute la conversation : session durable, les relances fonctionnent sans réexpliquer.

Tous les chiffres cités par l'agent proviennent des résultats d'outils (instructions strictes).

### Déploiement géré

```bash
cd agent
agents deploy --name csv-analyst   # nécessite le dépôt poussé + connecteur GitHub + clé API
```

Le déploiement durcit l'agent par défaut (le connecteur Drive agit au nom de l'utilisateur de session).

## 2. `workflow/` — Extraction du catalogue « Études en France » (Mistral Workflows)

Workflows durables qui extraient les formations **master / management** du
[catalogue Études en France](https://etudesenfrance.diplomatie.gouv.fr/catalogue-formations) :

| Workflow | Rôle |
| --- | --- |
| `efr_probe` | Sonde les 2 endpoints (liste paginée + détail) et documente leur schéma |
| `efr_list_extract` | Parcourt toute la liste paginée (`POST /parcours-api/public/catalogue/formations`), filtre par type de diplôme (Master, MSc, MS, MBA, diplôme d'école visé bac+4/5) et libellé |
| `efr_details_extract` | Récupère les détails par formation : **prérequis**, **frais de formation**, langue, domaine, contacts |

Le portail coupe le service en cas de sollicitation trop rapide : le client intègre
backoff et délais (voir `workflow/catalogue_client.py`).

### Déploiement géré

```bash
git push           # puis déployer depuis la révision voulue
```

Exécution depuis Vibe (`execute_workflow`) :

```json
{"workflow_identifier": "efr_probe", "input": {"page_size": 5}}
```

## Interaction via Vibe

- **Agent** : après déploiement, l'agent `csv-analyst` est disponible dans le chat Mistral
  (URL `managed-agents/csv-analyst` renvoyée par le déploiement) ou programmatiquement :
  `agents.Agent.load_from(agent_name="csv-analyst")` puis `agent.start_session()`.
- **Extraction** : suivi dans Mistral Studio → Workflows → exécutions.

## Configuration

Aucun secret n'est requis dans le dépôt. Voir `.env.example` pour les variables utiles en local.
