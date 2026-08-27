# Planificador CP-SAT — Streamlit Demo Alfa 1

Còpia desplegable de l'aplicació multipàgina. Inclou resum, planificació
incremental, pla publicat, hores realitzades, personal i incidències.

El formulari permet escollir entre el motor **Vigent** i **Nou per prioritats**.
El motor nou resol restriccions dures, cobertura, minuts coberts, estabilitat,
menys minuts acumulats, límit tou de ratxes fora de zona i preferències de
torn i zona.

## Mode del desplegament

La demo arrenca per defecte amb:

```powershell
$env:PLANIFICACIO_INCREMENTAL_MODE = "active"
$env:PLANIFICACIO_INCREMENTAL_PUBLICATION_ENABLED = "true"
```

Els valors s'apliquen amb `setdefault`, de manera que una variable externa pot
desactivar-los. Cada sessió publica únicament sobre la seva còpia temporal.

## Base de dades

El repositori inclou només:

```text
data/treballadors_demo.db
```

La demo s'ha regenerat a partir de
`treballadors_2025_absentisme_base.db`. Conserva el patró anual d'absentisme,
descans, històric i cobertura, però substitueix les identitats i desplaça totes
les dates uniformement.

La base original no s'inclou perquè conté camps identificatius i el repositori
és públic. El procés està documentat a
[`data/ANONIMITZACIO.md`](data/ANONIMITZACIO.md).

En iniciar l'aplicació, cada sessió rep una còpia temporal independent:

- les publicacions i incidències no modifiquen el fitxer del repositori;
- els canvis no es comparteixen entre sessions;
- el rollback es pot provar dins de la mateixa sessió;
- les dades temporals es poden perdre quan el servidor es reinicia.

Aquest comportament és adequat per a demostració, no per a producció.

## Execució local

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

Per provar una base externa:

```powershell
$env:PLANIFICADOR_DATABASE_PATH = "C:\ruta\treballadors.db"
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

## Verificació abans de publicar

```powershell
python scripts\verify_deploy.py --require-demo-data
```

El verificador comprova estructura, compilació, configuració `active`,
integritat SQLite, identitats sintètiques i absència de bases no autoritzades.

## Regeneració de la demo

Des d'aquest directori:

```powershell
python scripts\create_demo_database.py `
  --source ..\..\data\treballadors_2025_absentisme_base.db `
  --output data\treballadors_demo.db `
  --replace
python scripts\verify_deploy.py --require-demo-data
```

No s'ha de copiar mai la base original, backups, CSV de resultats ni fitxers
que permetin reconstruir la correspondència de les identitats.
