"""warm_catalog_cache ikki marta ketma-ket chaqirilganda yiqilmasligi.

Production logida har 10 daqiqada shu xato chiqib turgan:
  RuntimeError: got Future attached to a different loop
Sababi -- modul darajasidagi async dvigatel birinchi hodisa halqasiga
bog'lanib qoladi, vazifa esa har safar `asyncio.run()` bilan yangisini
ochadi. Bitta chaqiruv bilan sinab bo'lmaydi: xato IKKINCHISIDA chiqadi.
"""

from app.workers.tasks import maintenance


def test_two_runs_in_a_row_do_not_cross_event_loops() -> None:
    first = maintenance.warm_catalog_cache()
    second = maintenance.warm_catalog_cache()

    assert first >= 0
    assert second >= 0
