#!/usr/bin/env bash
# Prove the queue keeps a job when the worker dies.
#
# The claim: `task_acks_late` plus `task_reject_on_worker_lost` mean a job that
# was in flight when a worker was killed goes back to the queue and runs again.
# Everything downstream of a payment rests on that claim — it is why "paid but
# not delivered" is supposed to be impossible — and a claim nobody has tested is
# a hope.
#
# The drill uses a job that does nothing but take its time, so it can be run on
# the live system without touching an order, a customer or a supplier balance.
#
# Usage:  ./scripts/drill_worker_loss.sh            (run on the server)
#
# Reading the result: the log must show `diagnostics.started` twice for the same
# task id — once before the kill, once after the worker comes back.

set -euo pipefail

cd "${QULAYSIM_DIR:-$HOME/qulaysim}"
COMPOSE="sudo docker compose"

echo "== 1/5 · Sozlama tekshiriladi =="
$COMPOSE exec -T worker python - <<'PY'
from app.workers.celery_app import celery_app

conf = celery_app.conf
print(f"  task_acks_late            = {conf.task_acks_late}")
print(f"  task_reject_on_worker_lost = {conf.task_reject_on_worker_lost}")
assert conf.task_acks_late, "acks_late off: a killed worker loses its job"
assert conf.task_reject_on_worker_lost, "reject_on_worker_lost off: same problem"
print("  ✓ navbat kafolati yoqilgan")
PY

echo "== 2/5 · Uzoq vazifa navbatga qo'yiladi =="
TASK_ID=$($COMPOSE exec -T api python - <<'PY'
from app.workers.tasks.diagnostics import slow_noop

result = slow_noop.apply_async((30, "worker-loss-drill"))
print(result.id)
PY
)
TASK_ID=$(echo "$TASK_ID" | tr -d '\r' | tail -1)
echo "  task id: $TASK_ID"

echo "== 3/5 · Boshlanishini kutamiz =="
sleep 8
$COMPOSE logs worker --since 60s 2>&1 | grep -c "diagnostics.started" | sed 's/^/  boshlanishlar: /'

echo "== 4/5 · Worker o'ldiriladi (SIGKILL) =="
# SIGKILL, not a graceful stop: a graceful stop finishes the job, which would
# prove nothing. This is the crash case.
$COMPOSE kill -s SIGKILL worker
sleep 2
$COMPOSE up -d worker >/dev/null
echo "  worker qayta ko'tarildi"

echo "== 5/5 · Vazifa qaytdimi =="
sleep 40
STARTS=$($COMPOSE logs worker --since 180s 2>&1 | grep -c "diagnostics.started" || true)
FINISH=$($COMPOSE logs worker --since 180s 2>&1 | grep -c "diagnostics.finished" || true)
echo "  boshlanishlar: $STARTS   tugashlar: $FINISH"

if [ "$STARTS" -ge 2 ] && [ "$FINISH" -ge 1 ]; then
  echo
  echo "✓ MASHQ O'TDI — o'ldirilgan vazifa navbatdan qaytib, tugadi."
  echo "  Ya'ni to'lov kelib, worker o'sha payt yiqilsa ham eSIM yetkaziladi."
else
  echo
  echo "✗ MASHQ O'TMADI — vazifa qaytmadi."
  echo "  Bu 'pul olindi, eSIM yetmadi' holatining aynan sababi. Tekshirish kerak:"
  echo "  broker ulanishi, acks_late sozlamasi, worker'ning prefetch qiymati."
  exit 1
fi
