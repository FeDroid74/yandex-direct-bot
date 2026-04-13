# 2026-03-31 — campaign 708460300 update-first edit

- User requested UPDATE-FIRST for campaign `708460300`.
- Existing entities at start:
  - ad_group_id: `5736002597`
  - ad_ids: `17669222185`, `17669222884`
- Backend limitation confirmed: current backend supports `update_campaign`, `create_ad_group`, `create_ad`, but does **not** expose update endpoints for ad groups or ads, and deletion was explicitly forbidden.
- Applied best-effort non-destructive change in production with confirm=true:
  - updated campaign name to `Подарки | Фарфоровые статуэтки`
  - reused existing ad group `5736002597`
  - kept existing ads unchanged (cannot update via backend)
  - created 4 new ads in existing group: `17673987482`, `17673987696`, `17674002426`, `17674002436`
  - created 3 new ad groups: `5737399971`, `5737399993`, `5737399999`
  - created 12 ads in new groups
- Final ad_group_ids after operation:
  - `5736002597`, `5737399971`, `5737399993`, `5737399999`
- Final ad_ids after operation:
  - `17669222185`, `17669222884`, `17673987482`, `17673987696`, `17674002426`, `17674002436`, `17673987697`, `17673987698`, `17673987700`, `17673987734`, `17673987763`, `17673987764`, `17673987765`, `17673987859`, `17673987982`, `17673987983`, `17673988104`, `17673988105`
- Group `5736002597` now has 4 target ads, so it can be treated as structurally complete for later cleanup of legacy ads.
