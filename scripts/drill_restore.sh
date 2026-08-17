#!/usr/bin/env bash
# Restore last night's backup into a throwaway database and check it is real.
#
# An untested backup is not a backup. The nightly dump has been running for
# weeks; nobody has ever restored one, which means nobody knows whether it
# contains what it should or whether the file can even be read.
#
# This touches nothing live: the dump is restored into a temporary Postgres
# container on a different port, checked, and thrown away. Safe to run any time,
# and the review asks for it once a quarter.
#
# Usage:  ./scripts/drill_restore.sh            (run on the server)

set -euo pipefail

cd "${QULAYSIM_DIR:-$HOME/qulaysim}"
COMPOSE="sudo docker compose"
PROBE="qulaysim-restore-drill"
PROBE_PORT=55432

cleanup() {
  sudo docker rm -f "$PROBE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== 1/4 · Eng yangi zaxira topiladi =="
BACKUP_DIR=$($COMPOSE exec -T postgres-backup sh -c 'echo ${BACKUP_DIR:-/backups}' | tr -d '\r')
LATEST=$($COMPOSE exec -T postgres-backup sh -c "ls -t ${BACKUP_DIR}/*.sql.gz 2>/dev/null | head -1" | tr -d '\r')
if [ -z "$LATEST" ]; then
  echo "✗ Zaxira fayli topilmadi — bu allaqachon muammo."
  exit 1
fi
SIZE=$($COMPOSE exec -T postgres-backup sh -c "du -h '$LATEST' | cut -f1" | tr -d '\r')
echo "  $LATEST  ($SIZE)"

echo "== 2/4 · Vaqtinchalik baza ko'tariladi =="
sudo docker run -d --name "$PROBE" \
  -e POSTGRES_PASSWORD=drill -e POSTGRES_USER=drill -e POSTGRES_DB=drill \
  -p "${PROBE_PORT}:5432" postgres:16-alpine >/dev/null
for _ in $(seq 1 30); do
  if sudo docker exec "$PROBE" pg_isready -U drill >/dev/null 2>&1; then break; fi
  sleep 1
done
echo "  tayyor (port ${PROBE_PORT})"

echo "== 3/4 · Zaxira tiklanadi =="
$COMPOSE exec -T postgres-backup sh -c "gunzip -c '$LATEST'" \
  | sudo docker exec -i "$PROBE" psql -U drill -d drill -v ON_ERROR_STOP=0 >/dev/null 2>&1
echo "  tiklandi"

echo "== 4/4 · Ichida nima borligi tekshiriladi =="
# Row counts, not just "the file opened": a dump that restores an empty schema
# passes every check that only looks at exit codes.
sudo docker exec -i "$PROBE" psql -U drill -d drill -tA <<'SQL'
SELECT
  (SELECT count(*) FROM orders_order)        AS buyurtmalar,
  (SELECT count(*) FROM orders_esim)         AS esimlar,
  (SELECT count(*) FROM customers_customer)  AS mijozlar,
  (SELECT count(*) FROM catalog_plan)        AS tariflar,
  (SELECT count(*) FROM orders_order WHERE status = 'paid') AS tolangan;
SQL

ORDERS=$(sudo docker exec -i "$PROBE" psql -U drill -d drill -tA -c "SELECT count(*) FROM orders_order" | tr -d '\r')
PLANS=$(sudo docker exec -i "$PROBE" psql -U drill -d drill -tA -c "SELECT count(*) FROM catalog_plan" | tr -d '\r')

echo
if [ "${ORDERS:-0}" -gt 0 ] && [ "${PLANS:-0}" -gt 100 ]; then
  echo "✓ MASHQ O'TDI — zaxirada buyurtmalar ham, katalog ham bor."
  echo "  Tiklash yo'li ishlaydi: gunzip → psql, taxminan shu qadar vaqt oladi."
else
  echo "✗ MASHQ O'TMADI — zaxira ochildi, lekin ichi bo'sh yoki chala."
  echo "  buyurtmalar=${ORDERS}  tariflar=${PLANS}"
  echo "  Zaxira skriptini tekshirish kerak: qaysi bazani va qaysi jadvallarni oladi."
  exit 1
fi
