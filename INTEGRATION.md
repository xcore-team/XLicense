# Guide d'Intégration XLicense

Ce document explique comment intégrer et utiliser XLicense au sein de l'écosystème Xcore.

## 1. Utilisation du Middleware

XLicense injecte automatiquement des informations sur la licence du tenant actuel dans chaque requête traversant le middleware.

### Headers injectés
Toutes les requêtes sortant du middleware (vers votre plugin) contiennent les headers suivants :
- `X-License-Valid`: `true` ou `false`.
- `X-License-State`: L'état actuel (`active`, `trial`, `suspended`, etc.).
- `X-License-Tenant-ID`: L'ID du tenant associé.
- `X-License-Plan-ID`: L'ID du plan souscrit.

### Accès via `request.state` (FastAPI)
Si votre plugin utilise FastAPI, vous pouvez accéder à ces informations directement :
```python
@router.get("/my-feature")
async def my_feature(request: Request):
    is_valid = getattr(request.state, "license_valid", False)
    if not is_valid:
        raise HTTPException(status_code=402, detail="Licence requise")
    # ...
```

## 2. Communication Inter-Plugins (IPC)

Vous pouvez interagir avec XLicense depuis un autre plugin via le système d'actions de Xcore.

### Valider une licence
```python
result = await ctx.call("xlicense.validate", license_key="votre_cle_jwt")
# Retourne : {"valid": bool, "state": str, "license_id": str, ...}
```

### Récupérer les licences d'un tenant
```python
result = await ctx.call("xlicense.get_tenant_licenses", tenant_id="uuid-du-tenant")
# Retourne : {"ok": True, "licenses": [...]}
```

### Déclencher une transition d'état
```python
await ctx.call("xlicense.transition", 
    license_id="uuid-licence", 
    to_state="active", 
    reason="Paiement reçu"
)
```

## 3. Intégration Client (Agents/Machines)

Pour les applications externes (CLI, agents, serveurs distants), la validation se fait via l'endpoint public :

**Endpoint:** `POST /app/xlicense/verify`

**Payload:**
```json
{
  "license_key": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

**Réponse:**
```json
{
  "valid": true,
  "state": "active",
  "license_id": "...",
  "tenant_id": "...",
  "expires_at": "2026-05-21T12:00:00Z"
}
```

## 4. Gestion des Erreurs

- **402 Payment Required:** Renvoyé par le middleware si `config.enforce` est à `true` et qu'aucune licence active n'est trouvée pour le tenant.
- **403 Forbidden:** Problème de permissions lors de la gestion des licences.
- **422 Unprocessable Entity:** Paramètres de requête invalides.
