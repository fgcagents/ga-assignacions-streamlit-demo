# Prototip CP-SAT per a Streamlit Community Cloud

> **Estat documental:** còpia sincronitzada el 20/08/2026 amb l’aplicació
> multipàgina vigent, inclosos el resum, la planificació incremental, el pla
> publicat, la gestió de personal i les incidències. El motor incorpora el
> límit dur d'11 dies de treball consecutius. L'ordre de resolució és màxima
> cobertura, estabilitat opcional, equitat d'hores del grup T i desempat simple
> pel total de canvis de zona i torn. La referència contractual del grup T és
> el 75% de 1.605 hores i només es prorrateja per les absències pròpies; no és
> un mínim rígid ni s'amplia amb les baixes del grup A. Els diagnòstics es
> mantenen com a informació posterior, sense fases addicionals, índex compost,
> reintents dirigits ni portes d'aprovació manual.

Aquest directori és una còpia desplegable i independent del projecte de
treball. Conté l'aplicació multipàgina, els serveis que utilitza i el paquet
del solver CP-SAT.

## Seguretat de les dades

La base operativa `treballadors.db` no forma part d'aquest directori i no s'ha
de publicar a GitHub.

El projecte ja inclou una còpia pseudonimitzada amb aquesta ruta exacta:

```text
data/treballadors_demo.db
```

S'ha generat amb identitats sintètiques, patrons funcionals coherents, dates
desplaçades i estat operatiu buit. El procés i els controls es troben a
[`data/ANONIMITZACIO.md`](data/ANONIMITZACIO.md). `.gitignore` bloqueja
qualsevol altra base SQLite.

En iniciar l'aplicació, cada sessió rep una còpia temporal independent de la
base demo. Per tant:

- les proves d'un usuari no modifiquen el fitxer publicat;
- els canvis no es comparteixen entre sessions;
- les publicacions, incidències i rollback són simulacions temporals;
- les dades es poden perdre quan la sessió o el servidor es reinicien.

Aquest comportament és adequat per a demostració, però no per a producció.
Streamlit Community Cloud no garanteix la persistència dels fitxers locals.

## Execució local

Amb Python 3.13:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

També es pot provar temporalment amb una base externa sense copiar-la:

```powershell
$env:PLANIFICADOR_DATABASE_PATH = "C:\ruta\a\treballadors.db"
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

## Comprovació abans de publicar

Sense exigir encara la base demo:

```powershell
python scripts\verify_deploy.py
```

Just abans de pujar el repositori:

```powershell
python scripts\verify_deploy.py --require-demo-data
```

La comprovació valida l'estructura, compila tots els fitxers Python, comprova
la integritat i el patró sintètic de la base, i rebutja bases, còpies de
seguretat o resultats fora de la ruta demo autoritzada.

## Regeneració de la base demo

Només cal regenerar-la quan canvia l'esquema o quan es vol actualitzar el joc
de proves. Des de l'arrel d'aquest projecte:

```powershell
python scripts\create_demo_database.py `
  --source ..\treballadors.db `
  --output data\treballadors_demo.db `
  --replace
python scripts\verify_deploy.py --require-demo-data
```

El generador obre l'original en mode de només lectura, crea primer un fitxer
temporal i només substitueix la base demo si totes les validacions són
correctes. No genera cap fitxer de correspondències.

## Desplegament a Streamlit Community Cloud

1. Creeu un repositori de GitHub amb el contingut d'aquesta carpeta com a
   arrel.
2. Afegiu només `data/treballadors_demo.db` després de pseudonimitzar-la i
   revisar-la.
3. Executeu la comprovació amb `--require-demo-data`.
4. A Streamlit Community Cloud, seleccioneu el repositori i la branca.
5. Indiqueu `streamlit_app.py` com a fitxer principal.
6. A **Advanced settings**, seleccioneu Python 3.13, la mateixa versió amb què
   s'ha validat localment.
7. Desplegueu l'aplicació i reviseu les tres pantalles.

Les dependències estan fixades a `requirements.txt`. Community Cloud instal·la
les dependències d'aquest fitxer i torna a desplegar l'aplicació quan canvia el
repositori.

## Actualitzacions

No s'han d'implementar funcionalitats directament en aquesta còpia. Els canvis
es desenvolupen i validen al projecte de treball i només es promocionen aquí
després de l'acceptació. El procediment complet es troba a
[`PROTOCOL_ACTUALITZACIONS.md`](PROTOCOL_ACTUALITZACIONS.md).

Documentació oficial:

- https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy
- https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies
- https://docs.streamlit.io/develop/concepts/connections/connecting-to-data
