# mp-sync

Pipeline de ingesta Mercado Público -> Supabase (licitaciones, compra ágil, órdenes de compra, CRM).
Corre en GitHub Actions (ver `.github/workflows`). Configuración en `mp_sync/ORQUESTACION.md`.
Secretos (tickets MP, Supabase) van en GitHub Secrets, nunca en el código.
