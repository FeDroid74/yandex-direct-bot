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
    "campaign_name": "Клоуны | Approved",
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
        "group_name": "Клоуны",
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
            "title": "Клоуны | ArtFarfor",
            "text": "Клоуны из фарфора для интерьера, коллекции и подарка.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          },
          {
            "title": "Клоуны | ArtFarfor",
            "text": "Клоуны ручной работы для дома, витрины и коллекции.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          },
          {
            "title": "Клоуны | ArtFarfor",
            "text": "Клоуны для подарка и декора с доставкой по России.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          },
          {
            "title": "Клоуны | ArtFarfor",
            "text": "Клоуны для ценителей авторского и коллекционного фарфора.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          }
        ]
      },
      {
        "group_name": "Клоуны в подарок",
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
            "title": "Клоуны в подарок | ArtFarfor",
            "text": "Клоуны в подарок из фарфора для интерьера, коллекции и подарка.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          },
          {
            "title": "Клоуны в подарок | ArtFarfor",
            "text": "Клоуны в подарок ручной работы для дома, витрины и коллекции.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          },
          {
            "title": "Клоуны в подарок | ArtFarfor",
            "text": "Клоуны для подарка и декора с доставкой по России.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          },
          {
            "title": "Клоуны в подарок | ArtFarfor",
            "text": "Клоуны для ценителей авторского и коллекционного фарфора.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          }
        ]
      },
      {
        "group_name": "Клоуны для интерьера",
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
            "title": "Клоуны для интерьера | ArtFarfor",
            "text": "Клоуны для интерьера из фарфора для интерьера, коллекции и подарка.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          },
          {
            "title": "Клоуны для интерьера | ArtFarfor",
            "text": "Клоуны для интерьера ручной работы для дома, витрины и коллекции.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          },
          {
            "title": "Клоуны для интерьера | ArtFarfor",
            "text": "Клоуны для подарка и декора с доставкой по России.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          },
          {
            "title": "Клоуны для интерьера | ArtFarfor",
            "text": "Клоуны для ценителей авторского и коллекционного фарфора.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          }
        ]
      },
      {
        "group_name": "Клоуны для коллекции",
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
            "title": "Клоуны для коллекции | ArtFarfor",
            "text": "Клоуны для коллекции из фарфора для интерьера, коллекции и подарка.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          },
          {
            "title": "Клоуны для коллекции | ArtFarfor",
            "text": "Клоуны для коллекции ручной работы для дома, витрины и коллекции.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          },
          {
            "title": "Клоуны для коллекции | ArtFarfor",
            "text": "Клоуны для подарка и декора с доставкой по России.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          },
          {
            "title": "Клоуны для коллекции | ArtFarfor",
            "text": "Клоуны для ценителей авторского и коллекционного фарфора.",
            "final_url": "https://artfarfor.com",
            "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
            "ad_image_hashes": [
              "RxYJxGSusxMlWHl6TsybFQ",
              "I2eoI3vPqkEHA3W5c1dZUw"
            ],
            "creative_spec": null
          }
        ]
      }
    ],
    "ads": [
      {
        "title": "Клоуны | ArtFarfor",
        "text": "Клоуны из фарфора для интерьера, коллекции и подарка.",
        "final_url": "https://artfarfor.com",
        "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
        "ad_image_hashes": [
          "RxYJxGSusxMlWHl6TsybFQ",
          "I2eoI3vPqkEHA3W5c1dZUw"
        ],
        "creative_spec": null
      },
      {
        "title": "Клоуны в подарок | ArtFarfor",
        "text": "Клоуны в подарок ручной работы для дома, витрины и коллекции.",
        "final_url": "https://artfarfor.com",
        "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
        "ad_image_hashes": [
          "RxYJxGSusxMlWHl6TsybFQ",
          "I2eoI3vPqkEHA3W5c1dZUw"
        ],
        "creative_spec": null
      },
      {
        "title": "Клоуны для интерьера | ArtFarfor",
        "text": "Клоуны для подарка и декора с доставкой по России.",
        "final_url": "https://artfarfor.com",
        "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
        "ad_image_hashes": [
          "RxYJxGSusxMlWHl6TsybFQ",
          "I2eoI3vPqkEHA3W5c1dZUw"
        ],
        "creative_spec": null
      },
      {
        "title": "Клоуны для коллекции | ArtFarfor",
        "text": "Клоуны для ценителей авторского и коллекционного фарфора.",
        "final_url": "https://artfarfor.com",
        "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
        "ad_image_hashes": [
          "RxYJxGSusxMlWHl6TsybFQ",
          "I2eoI3vPqkEHA3W5c1dZUw"
        ],
        "creative_spec": null
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
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ"
    },
    {
      "type": "image",
      "source": "artfarfor_site",
      "theme": "Клоуны",
      "page_url": "https://artfarfor.com/collection/klouy-arlekiny-komedianty",
      "image_url": "https://static.insales-cdn.com/images/collections/1/5446/93631814/071124-03-O.jpg"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw"
    },
    {
      "type": "image",
      "source": "artfarfor_site",
      "theme": "Клоуны",
      "page_url": "https://artfarfor.com/product/statuetka-farfor-spokoynoy-nochi-mama-i-dochka-lladro-ispaniya",
      "image_url": "https://static.insales-cdn.com/images/products/1/8169/2941501417/060426-01-M.jpg"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "IHO-f9Il7KKnPDIq6v1Nng"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "Sstnne4bDKFjN1sZDITNCw"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "tgKMhP_QnxLNHpgx-uK52w"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "fw6muuCx9gnTqxCHJlNjBg"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "mlhdXdaKib9OQGDbCcl91w"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "8nmWYjvsFt5SSfA3YYnUUw"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "DpQJ67f8G_Ft45wE8i_ueQ"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "YWcpnYbO0iHZ38LuUnWhjg"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "1Mu74PCHpTIHFKVOUrzo4g"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "HRQBcQpxtcHHJbX_-K-IrA"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "OvgE0YVyJpyNtgu4x4JDmw"
    },
    {
      "type": "image",
      "source": "yandex_direct_upload",
      "ad_image_hash": "IB3ykYSU_kla8XM6p_4agQ"
    }
  ],
  "draft_media": [
    {
      "type": "image",
      "scope": "campaign",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_index": 0
    },
    {
      "type": "image",
      "scope": "campaign",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_index": 0
    },
    {
      "type": "image",
      "scope": "campaign",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_index": 1
    },
    {
      "type": "image",
      "scope": "campaign",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_index": 1
    },
    {
      "type": "image",
      "scope": "campaign",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_index": 2
    },
    {
      "type": "image",
      "scope": "campaign",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_index": 2
    },
    {
      "type": "image",
      "scope": "campaign",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_index": 3
    },
    {
      "type": "image",
      "scope": "campaign",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_index": 3
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 0,
      "ad_index": 0
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 0,
      "ad_index": 0
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 0,
      "ad_index": 1
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 0,
      "ad_index": 1
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 0,
      "ad_index": 2
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 0,
      "ad_index": 2
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 0,
      "ad_index": 3
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 0,
      "ad_index": 3
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 1,
      "ad_index": 0
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 1,
      "ad_index": 0
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 1,
      "ad_index": 1
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 1,
      "ad_index": 1
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 1,
      "ad_index": 2
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 1,
      "ad_index": 2
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 1,
      "ad_index": 3
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 1,
      "ad_index": 3
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 2,
      "ad_index": 0
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 2,
      "ad_index": 0
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 2,
      "ad_index": 1
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 2,
      "ad_index": 1
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 2,
      "ad_index": 2
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 2,
      "ad_index": 2
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 2,
      "ad_index": 3
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 2,
      "ad_index": 3
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 3,
      "ad_index": 0
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 3,
      "ad_index": 0
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 3,
      "ad_index": 1
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 3,
      "ad_index": 1
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 3,
      "ad_index": 2
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 3,
      "ad_index": 2
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "RxYJxGSusxMlWHl6TsybFQ",
      "ad_group_index": 3,
      "ad_index": 3
    },
    {
      "type": "image",
      "scope": "ad_group",
      "ad_image_hash": "I2eoI3vPqkEHA3W5c1dZUw",
      "ad_group_index": 3,
      "ad_index": 3
    }
  ],
  "last_uploaded_image_hash": "IB3ykYSU_kla8XM6p_4agQ",
  "creative_spec": null,
  "render_task": null,
  "render_result_url": null,
  "draft_meta": {
    "version": "v1",
    "last_action": "upload_ad_image",
    "theme": "Клоуны",
    "revision_count": 0,
    "supported_revision_rules": [
      "измени название кампании на X",
      "добавь группу X",
      "добавь минус-слово X"
    ]
  }
}
```
