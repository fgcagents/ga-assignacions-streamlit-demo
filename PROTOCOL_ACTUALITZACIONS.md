# Protocol de promoció de canvis

> **Promoció completada:** el 09/08/2026 s’han sincronitzat l’aplicació
> multipàgina, el nucli incremental i els modes de desplegament amb aquesta
> còpia de demostració.

## Carpetes

- **Projecte de treball:** arrel del repositori.
- **Projecte de proves:** `deploy/streamlit_demo`.

El projecte de treball és la font funcional. El projecte de proves és una
còpia desplegable destinada a demostració i validació d'usuaris.

## Regla de promoció

Un canvi només passa al projecte de proves quan:

1. s'ha implementat al projecte de treball;
2. supera les proves específiques i les bateries generals;
3. s'ha revisat funcionalment a Streamlit;
4. l'usuari l'ha acceptat explícitament;
5. no introdueix dades personals, resultats ni còpies de seguretat.

## Fitxers sincronitzats

| Projecte de treball | Projecte de proves |
|---|---|
| `streamlit_app.py` | `streamlit_app.py`, conservant la base temporal de sessió |
| `app_pages/` | mateixa ruta |
| `planificador_cp_sat/` | mateixa ruta |
| `cp_sat_pilot/src/cp_sat_pilot/` | mateixa ruta |

`streamlit_app.py` conserva una adaptació pròpia: utilitza una còpia temporal
per sessió de `data/treballadors_demo.db`. Aquesta adaptació no s'ha de perdre
quan es promocionin canvis de `streamlit_app.py`.

## Procediment després d'una acceptació

1. Identificar els fitxers afectats mitjançant la taula anterior.
2. Copiar únicament aquests fitxers al projecte de proves.
3. Reaplicar, si cal, l'adaptació de base temporal de `streamlit_app.py`.
4. Actualitzar `requirements.txt` si canvien les dependències.
5. Actualitzar el manual o aquest protocol si canvia el recorregut d'usuari.
6. Executar `python scripts/verify_deploy.py`.
7. Provar l'arrencada amb una còpia de la base de dades.
8. Si canvia l'esquema o la càrrega de dades, regenerar la base demo amb
   `scripts/create_demo_database.py` i revisar `data/ANONIMITZACIO.md`.
9. Incorporar el canvi al repositori de proves amb una descripció clara.

El push a GitHub es farà quan el repositori remot estigui configurat i l'usuari
ho demani o confirmi dins del flux de publicació.

## Fitxers que no es promocionen

- `treballadors.db` i qualsevol base operativa;
- `backups/`, còpies de seguretat o rollbacks;
- CSV, JSON, logs i resultats generats;
- entorns virtuals, memòria cau i fitxers compilats;
- eines, prototips o pantalles del GA no utilitzats per `streamlit_app.py`;
- proves que depenguin de dades reals.
