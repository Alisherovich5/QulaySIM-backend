# v1 → v2: nima o'zgardi va nega

Bu hujjat eski `app/` (2 270 qator, flat struktura) dan senior arxitekturaga
o'tishda tuzatilgan narsalarni sanab o'tadi. Har bir band — kodda topilgan
aniq muammo, umumiy maslahat emas.

## Bloklovchi/tuzatilgan xatolar

**1. Event loop bloklanishi — `webhooks.py:34`**
Eski handler `async def` edi, lekin ichida **sync** SQLAlchemy sessiyasi va
`synchronise_order()` chaqirig'i bor edi, u o'z navbatida `urlopen` bilan
provayderga HTTP so'rov yuborardi. Ya'ni provayder sekin javob bersa, butun
event loop — barcha boshqa so'rovlar bilan birga — qotardi.
→ Endi webhook faqat autentifikatsiya qiladi va ishni Celery'ga uzatadi.

**2. `needs_rehash()` runtime'da yiqilardi — `security.py`**
`bcrypt.gensalt().decode().split("$")[2].__int__()` — `str` da `__int__`
metodi yo'q. Funksiya chaqirilganda `AttributeError` berardi. Mypy strict
buni topdi.

**3. Register poygasi — `auth.py:21`**
Check-then-insert: ikki bir vaqtdagi so'rov `IntegrityError` → 500 berardi.
→ `flush()` + `IntegrityError` ushlash → 409.

**4. Email enumeration — ikki yo'l bilan**
- `register` "Email already registered" deb aniq aytardi.
- `login` da hisob topilmasa `verify_password` umuman chaqirilmasdi, ya'ni
  javob vaqti sezilarli farq qilardi.
→ Ikkalasi ham tuzatildi. O'lchandi: mavjud hisob 221 ms, mavjud emas 219 ms
  (farq 1.2%) — ikkalasi ham bcrypt bajaradi.

**5. `generate_code()` cheksiz sikl — `referral.py:14`**
`while True` ichida DB so'rovi, chiqish sharti yo'q. Ustiga TOCTOU poygasi.
→ 8 urinish bilan chegaralandi.

**6. Promo `used_count` poygasi**
Ikki bir vaqtdagi xarid `used_count < max_uses` tekshiruvidan ikkalasi ham
o'tishi mumkin edi. → `SELECT ... FOR UPDATE`.

**7. Referral mukofoti poygasi**
`reward_on_first_order` `count() == 1` ga tayanardi — ikki bir vaqtdagi
buyurtma ikkita mukofot yaratishi mumkin edi.
→ `FOR UPDATE skip_locked` bilan Celery vazifasiga ko'chirildi.

**8. eSIM hech qachon muddati tugamasdi**
Faollashtirilgan eSIM `expires_at` o'tgandan keyin ham `active` bo'lib
qolardi — hech narsa uni yangilamasdi. Account dashboard va Django admin
ikkalasi ham faol profillarni ortiqcha ko'rsatardi.
→ `maintenance.expire_esims` Celery beat vazifasi, har 15 daqiqada.

## Xavfsizlik

| Muammo | Yechim |
|---|---|
| `jwt_secret = "dev-secret"` default | Majburiy, min 32 belgi, placeholder'lar rad etiladi |
| `python-jose` (qo'llab-quvvatlanmaydi, CVE) | PyJWT |
| 7 kunlik yagona token, bekor qilib bo'lmaydi | 30 daq access + rotatsiyalanuvchi refresh + Redis denylist |
| Login/register'da rate limit yo'q | Redis sliding window, IP **va** hisob bo'yicha |
| Xavfsizlik sarlavhalari yo'q | nosniff, DENY, Referrer-Policy, Permissions-Policy, HSTS |
| `/docs` production'da ochiq | Production'da o'chirilgan |
| CORS localhost regex production'da | Faqat non-production'da |
| Xato javoblarida ichki ma'lumot | Yagona konvert, 500'da hech narsa oshkor bo'lmaydi |

## Performance

**Katalog N+1 → agregat.** Eski `/countries` har bir davlat uchun **barcha
tariflarni** `joinedload` bilan yuklab, faqat eng arzon narxni hisoblardi.
→ `GROUP BY` subquery. Javob hajmi endi davlatlar soniga proporsional.

**Savat narxlash N+1 → bitta so'rov.** Har bir savat qatori uchun alohida
`db.get(Plan, ...)` edi. → `WHERE id IN (...)`. Test bu regressiyani qo'riqlaydi.

**Kesh: jarayon ichidagi dict → Redis.** `currency.py` va `support.py` modul
darajasidagi lug'atlardan foydalanardi — bu ko'p worker'da ishlamaydi va
`support.py` dagi `_recent_requests` hech qachon tozalanmasdi (xotira oqishi).
→ Redis. O'lchandi: `/api/countries` 13.7 ms → 4.0 ms.

**Sync → async.** Butun stack `asyncpg` + `AsyncSession`. Tashqi HTTP
`urllib` dan `httpx` ga.

**Pul: `float` → `Decimal`.** Modellarda `Mapped[float]` ustida `Numeric(10,2)`
e'lon qilingan edi. Endi hamma joyda `Decimal`, JSON chegarasida float'ga
seriyalanadi (frontend kontrakti shuni kutadi).

## Django bilan kontrakt

`auto_now_add` — Django'ning **ORM darajasidagi** defaulti; bazada `created_at`
`NOT NULL` va **DEFAULT yo'q**. Ya'ni bu servisdan har bir INSERT `created_at`
ni o'zi berishi shart. Bu 6 ta modelga tegishli va endi test bilan qo'riqlanadi
(`test_schema_contract.py`), shuning uchun kimdir Python defaultini olib
tashlasa CI yiqiladi, production emas.

Xuddi shu test jadval/ustun/nullability mosligini ham tekshiradi. CI Django
migratsiyalarini `QulaySIM-admin` dan qo'llab, keyin shu testni ishga tushiradi.

## Infratuzilma (avval umuman yo'q edi)

- **Docker**: ko'p bosqichli build, non-root `app` foydalanuvchi, healthcheck,
  gunicorn + uvicorn worker'lar. Image 461 MB.
- **docker compose**: api + worker + beat + postgres + redis (+ Django admin
  `full` profilida).
- **Celery**: 6 vazifa, beat jadvali, eksponensial backoff, `acks_late`.
- **CI**: ruff + format + mypy strict + testlar (Django migratsiyalari bilan)
  + Docker build.
- **Testlar**: 0 dan 96 ga, 74% qamrov.

## Tekshirilgan holat

```
ruff check      All checks passed
ruff format     78 files already formatted
mypy --strict   Success: no issues found in 68 source files
pytest          96 passed, 74% coverage
docker build    Successfully tagged qulaysim-api:test (461 MB)
konteyner       healthy, user=app, /docs → 404, HSTS mavjud
frontend kontrakti  8/8 tip to'liq mos
```
