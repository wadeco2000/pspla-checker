# Actuate Schedule & Flex Schedule API Guide

Developer reference for building schedule management UI against the Actuate API.

---

## Authentication

All requests require the `Authorization` header:
```
Authorization: Token YOUR_ACTUATE_API_TOKEN
Content-Type: application/json
```

Base URL: `https://admin.actuateui.net/api/`

---

## Key Concepts

### Schedule Model
- Each site (customer) has one or more **schedule** records
- Each schedule defines: start time, end time, which days of the week, enabled/disabled
- Schedules support **overnight ranges** (e.g. 23:00 start, 01:00 end)
- If a site has different times on different days, Actuate creates **separate schedule records per unique time range** (e.g. one for Mon-Fri 22:00-06:00, another for Sat-Sun 20:00-06:00)

### Flex Schedule Model
- A **flex schedule** is a container that groups schedules and adds "arm after last motion" behaviour
- Regular schedules are linked to a flex schedule via the `flex_schedule` field on the schedule record
- When `flex_schedule` is set (integer ID), the schedule uses flex behaviour
- When `flex_schedule` is null, it uses fixed-time behaviour
- Toggling flex ON/OFF = setting/clearing the `flex_schedule` field on schedule records
- The flex schedule object itself holds: display name, product IDs (AI analytics), and its own schedule rules

### Critical API Quirks
1. **PATCH creates new records**: PATCHing a schedule always creates a NEW record with a new ID. The old record is sometimes auto-deleted, sometimes not. Always delete the old ID after a successful PATCH.
2. **Phantom schedules**: If a schedule doesn't cover all 7 days, Actuate auto-creates a "phantom" schedule for the remaining days with `start_time: null`. Delete these after any create/update operation.
3. **POST also creates phantoms**: Same behaviour as PATCH — partial day coverage triggers phantom creation.
4. **Required fields on PATCH**: `customer`, `day_of_week`, `start_time`, `end_time`, `enabled`, `always_on`, `buffer_time` are all required even for a partial update. Always GET the full record first, modify the field you need, then PATCH the whole object back.
5. **Schedule IDs are unstable**: IDs change on every PATCH/POST. Never cache or rely on schedule IDs persisting.

---

## Endpoints

### 1. List Schedules for a Site

```
GET /api/schedule/?customer__id={site_id}
```

Response (paginated):
```json
{
  "count": 2,
  "results": [
    {
      "id": 200604,
      "customer": 35218,
      "start_time": "23:00:00",
      "end_time": "01:00:00",
      "always_on": false,
      "day_of_week": ["0", "1", "2", "3", "4", "5", "6"],
      "schedule_status": true,
      "is_override": false,
      "location_dusk_dawn": null,
      "enabled": true,
      "override_start_date": null,
      "override_end_date": null,
      "buffer_time": 0,
      "flex_schedule": null
    }
  ]
}
```

**Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `id` | int | Schedule ID (changes on every update) |
| `customer` | int | Site/customer ID |
| `start_time` | string | HH:MM:SS format, null for phantoms |
| `end_time` | string | HH:MM:SS format |
| `always_on` | bool | If true, armed 24/7 regardless of times |
| `day_of_week` | array | Days: "0"=Sunday, "1"=Monday ... "6"=Saturday |
| `enabled` | bool | Whether this schedule is active |
| `is_override` | bool | True = temporary override (e.g. holiday schedule) |
| `override_start_date` | string | Date range for overrides (YYYY-MM-DD) |
| `override_end_date` | string | Date range for overrides |
| `buffer_time` | int | Minutes buffer before/after schedule |
| `flex_schedule` | int/null | If set, links to a flex schedule ID |
| `schedule_status` | bool | Internal status flag |

**Notes:**
- Filter out `is_override: true` schedules for normal display (they're for holidays)
- Filter out `enabled: false` schedules unless showing disabled/expired overrides
- Phantom schedules have `start_time: null` — delete these, don't display them

### 2. Get Single Schedule

```
GET /api/schedule/{schedule_id}/
```

Returns the full schedule object (same fields as above, not wrapped in results).

**Allowed methods:** `GET, PATCH, DELETE`

### 3. Create Schedule

```
POST /api/schedule/
```

Body:
```json
{
  "customer": 35218,
  "start_time": "22:00:00",
  "end_time": "06:00:00",
  "always_on": false,
  "day_of_week": ["0", "1", "2", "3", "4", "5", "6"],
  "enabled": true,
  "buffer_time": 0
}
```

Response: `201 Created` — returns array with the new schedule object.

**Warning:** If `day_of_week` doesn't include all 7 days, a phantom schedule is created for the missing days. Delete the phantom afterwards.

### 4. Update Schedule (PATCH)

```
PATCH /api/schedule/{schedule_id}/
```

Body: Full schedule object with desired changes. Must include at minimum: `customer`, `day_of_week`, `start_time`, `end_time`, `enabled`, `always_on`, `buffer_time`.

**Recommended workflow:**
```
1. GET /api/schedule/{id}/          → get full current object
2. Remove 'id' from the object
3. Modify desired fields
4. PATCH /api/schedule/{id}/        → sends modified object
5. Response: 201 Created            → new schedule with NEW id
6. DELETE /api/schedule/{old_id}/   → clean up old record (may return 404 if auto-deleted)
7. GET /api/schedule/?customer__id={site_id}  → check for phantoms
8. DELETE any schedules where start_time is null
```

### 5. Delete Schedule

```
DELETE /api/schedule/{schedule_id}/
```

Response: `200 OK`

### 6. List Flex Schedules

```
GET /api/flex_schedule/
```

Response (paginated):
```json
{
  "results": [
    {
      "id": 1189,
      "log_name": "fs-1189",
      "customer": 35218,
      "display_name": "Schedule 1",
      "is_running": false,
      "next_run": 1774515600.0,
      "product": [43],
      "schedule": [
        {
          "id": 200573,
          "customer": 35218,
          "start_time": "22:00:00",
          "end_time": "04:00:00",
          ...full schedule object...
        }
      ]
    }
  ]
}
```

**Fields:**
| Field | Type | Description |
|-------|------|-------------|
| `id` | int | Flex schedule ID |
| `customer` | int | Site/customer ID |
| `display_name` | string | User-friendly name |
| `is_running` | bool | Read-only — whether flex is currently active |
| `next_run` | float/null | Unix timestamp of next scheduled run |
| `product` | array | AI analytics product IDs (see Product IDs table below) |
| `schedule` | array | Schedule rules within this flex schedule |

**Filtering by site:** The `customer` filter doesn't work on the list endpoint. Filter client-side by `customer` field, or use `GET /api/flex_schedule/{id}/` if you know the ID.

### 7. Get/Update Flex Schedule

```
GET /api/flex_schedule/{flex_id}/
PATCH /api/flex_schedule/{flex_id}/
```

**Allowed methods:** `GET, PATCH, DELETE`

PATCH body — update display name and products:
```json
{
  "display_name": "Night Schedule",
  "product": [43, 48]
}
```

**Warning:** Do NOT send `schedule: []` in PATCH — this creates a phantom schedule. Only send `display_name` and/or `product` fields.

### 8. Creating Flex Schedules

```
POST /api/flex_schedule/
```

Returns `403` with message: "Create function is not offered in this path, use enable to enable this functionality instead."

**You cannot create new flex schedules via API.** They must be created in the Actuate portal first. You can only modify existing ones.

---

## Toggling Flex Schedule On/Off

This is the most complex operation. Here's the exact process:

### Toggle ON (enable flex scheduling)

```python
# 1. Get all enabled, non-override schedules for the site
schedules = GET /api/schedule/?customer__id={site_id}
targets = [s for s in schedules if s.enabled and not s.is_override and s.flex_schedule is None]

# 2. For each schedule, link it to the flex schedule
for schedule in targets:
    old_id = schedule.id

    # 3. Get full schedule object
    full = GET /api/schedule/{old_id}/

    # 4. Remove read-only fields, set flex_schedule
    del full['id']
    full['flex_schedule'] = flex_schedule_id  # e.g. 1189

    # 5. PATCH — creates new record
    result = PATCH /api/schedule/{old_id}/  (body: full)
    new_id = result[0]['id']

    # 6. Delete old record if it still exists
    DELETE /api/schedule/{old_id}/  # may return 404, that's OK

# 7. Clean up phantoms
all_schedules = GET /api/schedule/?customer__id={site_id}
for s in all_schedules:
    if s.enabled and not s.is_override and s.start_time is None:
        DELETE /api/schedule/{s.id}/

# 8. Deploy to apply changes
POST /api/customer/{site_id}/action/deploy_now/
```

### Toggle OFF (disable flex scheduling)

Same process but set `flex_schedule: null` instead of the flex ID:
```python
targets = [s for s in schedules if s.enabled and not s.is_override and s.flex_schedule == flex_id]
# ... same PATCH/DELETE flow but with full['flex_schedule'] = None
```

---

## Deploy After Changes

After any schedule change, Actuate requires a deploy to apply the settings:

```
POST /api/customer/{site_id}/action/deploy_now/
```

Body: `{}` (empty JSON)

Response: `200 OK` with message like `"Settings deployed for Site Name"`

This is equivalent to "Deploy Settings - Now" in the Actuate portal. The site will start immediately.

---

## Product IDs (AI Analytics)

These are the `product` IDs used in flex schedules and sensitivity settings:

| ID | Name | Value |
|----|------|-------|
| 43 | Intruder | intruder |
| 44 | Slip and Fall | fall |
| 45 | All | All |
| 47 | Crowd | crowd |
| 48 | Loitering | loiterer |
| 50 | Gun | gun |
| 51 | False Positive Reduction | fp_reduction |
| 59 | Postal Vehicle ID | postal_vehicle_id |
| 60 | Summary | summary |
| 109 | Vehicle Loitering | vehicle_loiterer |
| 117 | Hard Hat | no_hardhat |
| 139 | Fire | fire |
| 195 | Package | package |
| 258 | Health Monitoring | healthcheck |
| 260 | Non-Postal | non_postal |
| 325 | Motion + | motion_plus |
| 339 | Line Crossing | line_crossing |

Source: `GET /api/option/` filtered by `parent: "analytics_product"`

---

## Site/Customer Info

### Get site details (used for armed status, last alert, etc.)

```
GET /api/customer/{site_id}/about/
```

Key fields:
| Field | Type | Description |
|-------|------|-------------|
| `active` | bool | Site is active |
| `armed` | bool | Site is currently armed |
| `armed_status` | string | "OK" or "WARNING" |
| `deployed_date` | float | Unix timestamp of last deploy |
| `last_motion` | float | Unix timestamp of last motion detected |
| `last_alert` | float | Unix timestamp of last alert |
| `motion_percentage` | float | Current motion percentage (0.0 to 1.0) |
| `parent_group` | object | `{id, name}` — the group/dealer this site belongs to |
| `has_flex_schedule` | bool | Whether flex scheduling is enabled |
| `integration_type_name` | string | Camera integration type |
| `breadcrumbs` | array | Full group hierarchy path |

### Get camera status for a site

```
GET /api/camera/site/?customer__id={site_id}
```

Returns cameras with `active`, `deployed`, `health_status` fields. Use to count active vs inactive cameras.

### Get Patriot number for a site

```
GET /api/camera/{camera_id}/general_info/
```

The Patriot client number is nested inside:
```
response.streams[].patriot_alerts[].patriot_client_no
```

You need to get a camera ID first from `GET /api/camera/?customer__id={site_id}`, then query `general_info/` on the first camera.

---

## Day of Week Mapping

Actuate uses string numbers for days:
| Value | Day |
|-------|-----|
| "0" | Sunday |
| "1" | Monday |
| "2" | Tuesday |
| "3" | Wednesday |
| "4" | Thursday |
| "5" | Friday |
| "6" | Saturday |

---

## Endpoint Permissions Summary

| Endpoint | List | Item |
|----------|------|------|
| `schedule` | GET, POST | GET, PATCH, DELETE |
| `flex_schedule` | GET, POST* | GET, PATCH, DELETE |
| `customer` | GET, POST | GET, PATCH, DELETE |
| `camera` | GET, POST | GET, DELETE, PATCH |
| `camera/site/` | GET | — |
| `camera/{id}/general_info/` | GET | — |
| `camera/{id}/status/` | GET | — |
| `customer/{id}/about/` | GET | — |
| `customer/{id}/action/{action}/` | — | POST |
| `option` | GET | — |
| `sensitivity` | GET | — |
| `camera_type` | GET | — |
| `group` | GET, POST | GET, PATCH |

*flex_schedule POST returns 403 — use Actuate portal to create new flex schedules.

---

## Example: Complete Schedule Update Flow

```python
import requests

BASE = "https://admin.actuateui.net/api"
HEADERS = {"Authorization": "Token YOUR_TOKEN", "Content-Type": "application/json"}
SITE_ID = 35218

# 1. Get current schedules
r = requests.get(f"{BASE}/schedule/", params={"customer__id": SITE_ID}, headers=HEADERS)
schedules = r.json().get("results", r.json())

# 2. Find the active schedule (ignore disabled/overrides/phantoms)
active = [s for s in schedules if s["enabled"] and not s.get("is_override") and s.get("start_time")]

if active:
    sched = active[0]
    old_id = sched["id"]

    # 3. Modify the schedule
    sched.pop("id")
    sched.pop("location_dusk_dawn", None)
    sched.pop("override_start_date", None)
    sched.pop("override_end_date", None)
    sched["start_time"] = "22:00:00"  # Change start time
    sched["end_time"] = "06:00:00"    # Change end time

    # 4. PATCH
    r2 = requests.patch(f"{BASE}/schedule/{old_id}/", headers=HEADERS, json=sched)
    new_sched = r2.json()
    new_id = new_sched[0]["id"] if isinstance(new_sched, list) else new_sched["id"]

    # 5. Delete old (may 404)
    requests.delete(f"{BASE}/schedule/{old_id}/", headers=HEADERS)

    # 6. Clean phantoms
    r3 = requests.get(f"{BASE}/schedule/", params={"customer__id": SITE_ID}, headers=HEADERS)
    for s in r3.json().get("results", []):
        if s.get("enabled") and not s.get("is_override") and s.get("start_time") is None:
            requests.delete(f"{BASE}/schedule/{s['id']}/", headers=HEADERS)

    # 7. Deploy
    requests.post(f"{BASE}/customer/{SITE_ID}/action/deploy_now/", headers=HEADERS, json={})
```

---

## Our Implementation

In the PSPLA Checker dashboard (`dashboard.py`), the schedule editor is implemented with:

**Backend endpoints:**
- `POST /api/actuate/schedule-update` — proxies PATCH to Actuate, handles old ID deletion + phantom cleanup
- `POST /api/actuate/flex-toggle` — toggles flex on/off for all schedules on a site
- `GET /api/actuate/query?path=schedule/&site_id=X` — generic proxy for GET requests

**Frontend (JS in dashboard.py template):**
- `loadSchedules()` — fetches schedules + flex schedules for current site
- `renderScheduleGrid()` — draws the day/hour grid with green armed cells
- `schedDragStart/Move/End()` — drag-to-select hours on the grid
- `_applySelectedHours()` — converts selected cells to start/end times
- `saveSchedule()` — sends changes to backend
- `toggleFlexSchedule()` — calls flex-toggle endpoint
- `saveFlexSettings()` — saves flex display name + products
