# XLicense

XLicense est un plugin de gestion de licences entreprise pour l'écosystème Xcore. Il permet de gérer le cycle de vie complet des licences par tenant, d'émettre des clés JWT sécurisées et de protéger les routes de l'application via un middleware global.

## Fonctionnalités Clés

- **Cycle de Vie (State Machine) :** Passage automatique ou manuel entre les états `TRIAL`, `ACTIVE`, `SUSPENDED`, `EXPIRED` et `REVOKED`.
- **Sécurité JWT :** Génération de clés de licence signées en RS256.
- **Protection Middleware :** Injection automatique de l'état de la licence dans les requêtes et blocage (HTTP 402) des accès si aucune licence valide n'est présente.
- **Cache Haute Performance :** Utilisation du cache Redis de Xcore pour minimiser les accès à la base de données.
- **IPC (Inter-Process Communication) :** Actions disponibles pour les autres plugins pour valider ou modifier l'état des licences.
- **REST API :** Gestion complète des plans et des licences.

## Installation

Le plugin doit être installé dans l'environnement Xcore. Assurez-vous d'avoir les clés RSA nécessaires pour la signature des tokens.

### Configuration

La configuration se fait via le fichier `plugin.yaml` et les variables d'environnement.

#### Variables d'environnement
- `JWT_PRIVATE_KEY_PATH`: Chemin vers la clé privée RSA (.pem) pour la signature.
- `JWT_PUBLIC_KEY_PATH`: Chemin vers la clé publique RSA (.pem) pour la vérification.

#### plugin.yaml
```yaml
config:
  enforce: true # Active le blocage 402
  excluded_prefixes:
    - "/docs"
    - "/health"
    # ... autres routes publiques
```

## API REST

### Licences
- `POST /app/xlicense/licenses`: Créer une nouvelle licence.
- `GET /app/xlicense/licenses/tenant/{tenant_id}`: Lister les licences d'un tenant.
- `GET /app/xlicense/licenses/{license_id}`: Détails d'une licence.
- `POST /app/xlicense/licenses/{license_id}/activate`: Activer.
- `POST /app/xlicense/licenses/{license_id}/suspend`: Suspendre.
- `POST /app/xlicense/licenses/{license_id}/renew`: Renouveler.

### Public
- `POST /app/xlicense/verify`: Vérifier une clé de licence (utilisé par les agents externes).

## Développement

### Structure du projet
- `src/models`: Définition des schémas SQLAlchemy.
- `src/services`: Logique métier (JWT, validation, transitions).
- `src/state_machine.py`: Logique de transition d'états.
- `src/middleware.py`: Middleware ASGI de protection.

### Tests
(Ajoutez ici les instructions pour lancer les tests si disponibles)
