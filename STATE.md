# STATE.md - Campaign Draft State

## Session Modes

- draft_campaign
- review_draft
- analyze_campaign
- awaiting_confirm_create
- awaiting_confirm_update

## Current State

```json
{
  "session_mode": "review_draft",
  "draft_campaign": {
    "campaign_type": "UNIFIED_CAMPAIGN",
    "campaign_name": "Royal Doulton | Approved",
    "site_url": "https://artfarfor.com",
    "region": "RU",
    "language": "RU",
    "placement_type": "both",
    "goal_type": "leads",
    "strategy_type": "pay_for_conversion",
    "metrica_goal_id": 352606262,
    "metrica_counter_id": 99041859,
    "weekly_budget_rub": 10000,
    "target_cpa_rub": 500,
    "tracking_params": "utm_source=yandex&utm_medium=cpc&utm_campaign={campaign_id}&utm_content={ad_id}&utm_term={keyword}",
    "utm_tracking": true,
    "negative_keywords": [
      "оптом",
      "б/у"
    ],
    "sitelinks": [
      {
        "title": "Каталог",
        "href": "https://artfarfor.com"
      },
      {
        "title": "Коллекции",
        "href": "https://artfarfor.com"
      }
    ],
    "ad_groups": [
      {
        "group_name": "Статуэтки Royal Doulton в подарок",
        "negative_keywords": [],
        "autotargeting_settings": {
          "Categories": {
            "Exact": "YES",
            "Narrow": "YES",
            "Alternative": "YES",
            "Accessory": "YES",
            "Broader": "YES"
          },
          "BrandOptions": {
            "WithoutBrands": "YES",
            "WithAdvertiserBrand": "YES",
            "WithCompetitorsBrand": "NO"
          }
        },
        "ads": [
          {
            "title": "Royal Doulton | ArtFarfor",
            "text": "Статуэтки Royal Doulton — красивый подарок и коллекционный английский фарфор.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": null,
            "creative_spec": null,
            "ad_image_hashes": []
          }
        ]
      }
    ],
    "ads": [
      {
        "title": "Royal Doulton | ArtFarfor",
        "text": "Royal Doulton — коллекционная статуэтка и подарок из английского фарфора.",
        "final_url": "https://artfarfor.com",
        "ad_image_hash": "A3xEP1DlR40pUpIg9F5mYg",
        "creative_spec": null,
        "ad_image_hashes": [
          "A3xEP1DlR40pUpIg9F5mYg",
          "8I8g30Soojev2uJ5yhAv4A",
          "MAXDUmRpCDp93BtHPPJJuQ"
        ]
      }
    ],
    "autotargeting_settings": {
      "Categories": {
        "Exact": "YES",
        "Narrow": "YES",
        "Alternative": "YES",
        "Accessory": "YES",
        "Broader": "YES"
      },
      "BrandOptions": {
        "WithoutBrands": "YES",
        "WithAdvertiserBrand": "YES",
        "WithCompetitorsBrand": "NO"
      }
    },
    "assumptions": [
      "Assumption: used fixed site_url https://artfarfor.com per project restriction.",
      "Assumption: region='RU' and goal_type='leads' are draft defaults for backend validate/create flow and require review.",
      "Assumption: used RU, both placements, pay_for_conversion, metrica_goal_id 352606262 per confirmed project constraints.",
      "Assumption: weekly_budget_rub=10000 and target_cpa_rub=500 are draft defaults and require review.",
      "Assumption: sitelinks and negative keywords are prototype placeholders for review_draft."
    ]
  },
  "campaign_payload": null,
  "validation_result": null,
  "created_campaign_id": null,
  "last_plan": null,
  "last_proposal": null,
  "proposal_history": [],
  "approved_patterns": {
    "negative_keywords_defaults": [
      "оптом",
      "б/у"
    ],
    "sitelinks_defaults": null,
    "autotargeting_defaults": null,
    "campaign_name_suffix": " | Approved",
    "budget_defaults": null
  },
  "media_library": [
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "8I8g30Soojev2uJ5yhAv4A"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "MAXDUmRpCDp93BtHPPJJuQ"
    },
    {
      "type": "image",
      "source": "artfarfor_site",
      "theme": "Royal Doulton",
      "page_url": "https://artfarfor.com/collection/katalog-1-3cecf7",
      "image_url": "https://static.insales-cdn.com/images/collections/1/5447/93631815/211218-01-O.jpg"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "MAXDUmRpCDp93BtHPPJJuQ"
    },
    {
      "type": "image",
      "source": "artfarfor_site",
      "theme": "Royal Doulton",
      "page_url": "https://artfarfor.com/collection/katalog-1-3cecf7",
      "image_url": "https://static.insales-cdn.com/images/collections/1/5447/93631815/211218-01-O.jpg"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "A3xEP1DlR40pUpIg9F5mYg"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "MBt-Q6Kx_sKkHgGh5G1kBg"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "q1qj_BfZW0VhLEE1U4S8LQ"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "hoTCpDPWufvTuIaKEdG-3A"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "ccxxhnVHdFITZDeVQWwZDw"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "MAXDUmRpCDp93BtHPPJJuQ"
    },
    {
      "type": "image",
      "source": "artfarfor_site",
      "theme": "Royal Doulton",
      "page_url": "https://artfarfor.com/collection/katalog-1-3cecf7",
      "image_url": "https://static.insales-cdn.com/images/collections/1/5447/93631815/211218-01-O.jpg"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "A3xEP1DlR40pUpIg9F5mYg"
    },
    {
      "type": "image",
      "source": "artfarfor_site",
      "theme": "Royal Doulton",
      "page_url": "https://artfarfor.com/product/statuetka-farfor-tkach-snov-royal-doulton-angliya",
      "image_url": "https://static.insales-cdn.com/images/products/1/2497/2694662593/250126-03-O.jpg"
    }
  ],
  "draft_media": [
    {
      "type": "image",
      "scope": "campaign",
      "ad_image_hash": "MAXDUmRpCDp93BtHPPJJuQ",
      "ad_index": 0
    },
    {
      "type": "image",
      "scope": "campaign",
      "ad_image_hash": "probe",
      "ad_index": 0
    },
    {
      "type": "image",
      "scope": "campaign",
      "ad_image_hash": "A3xEP1DlR40pUpIg9F5mYg",
      "ad_index": 0
    },
    {
      "type": "image",
      "scope": "campaign",
      "ad_image_hash": "8I8g30Soojev2uJ5yhAv4A",
      "ad_index": 0
    }
  ],
  "last_uploaded_image_hash": "A3xEP1DlR40pUpIg9F5mYg",
  "creative_spec": null,
  "render_task": null,
  "render_result_url": null,
  "draft_meta": {
    "version": "v1",
    "last_action": "apply_site_image_to_draft_ad",
    "theme": "Royal Doulton",
    "revision_count": 12,
    "supported_revision_rules": [
      "измени название кампании на X",
      "добавь группу X",
      "добавь минус-слово X"
    ]
  }
}
```
