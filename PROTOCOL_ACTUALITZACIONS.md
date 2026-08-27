# Protocol de promoció de canvis

## Estat Alfa 1

La còpia desplegable inclou l'aplicació multipàgina, el tancament d'hores
realitzades, el selector de motors i el `PriorityPlanner`. El motor vigent
continua seleccionat per defecte.

La base pseudonimitzada es regenera des de
`treballadors_2025_absentisme_base.db`. L'aplicació arrenca en mode `active`
amb publicació habilitada, sempre sobre una còpia temporal per sessió.

## Carpetes sincronitzades

- `app_pages/`
- `planificador_cp_sat/`
- `cp_sat_pilot/src/cp_sat_pilot/`

El projecte de treball és la font funcional. Aquest repositori conserva només
les adaptacions de desplegament a `streamlit_app.py`, la base pseudonimitzada i
els seus controls.

## Regla de promoció

Un canvi passa a la demo quan:

1. està implementat al projecte de treball;
2. supera les proves específiques;
3. s'han sincronitzat les tres carpetes anteriors sense `__pycache__` ni `.pyc`;
4. s'ha regenerat la base demo si ha canviat l'escenari;
5. `python scripts\verify_deploy.py --require-demo-data` acaba correctament;
6. no s'inclouen dades personals, backups ni resultats operatius.

## Abans del commit

```powershell
git status --short
git diff --check
python scripts\verify_deploy.py --require-demo-data
```

La llista de canvis no ha de contenir `__pycache__`, `.pyc` ni cap SQLite fora
de `data/treballadors_demo.db`.
