# Registre de pseudonimització de la base demo

## Resultat

La base `treballadors_demo.db` es va generar l'1 d'agost de 2026 amb
`scripts/create_demo_database.py` i la llavor reproduïble `20260801`.
L'original s'obre en mode de només lectura i no es modifica.

La base resultant conté:

- 97 persones sintètiques: 50 del grup A, 32 del grup T i 15 del grup V;
- 11.036 registres de descans transformats;
- 449 registres d'històric transformats;
- 135 assignacions de referència transformades per conservar el cas de prova;
- 348 necessitats de cobertura i 365 dies de calendari desplaçats;
- cap incidència, esborrany, auditoria ni publicació anterior.

## Transformacions aplicades

1. Els identificadors i noms personals s'han substituït per valors sintètics
   (`Persona D001`, etc.).
2. Cada identitat sintètica conserva coherentment la rotació, la zona, les
   habilitacions, el grup i la resta d'atributs funcionals de la persona que
   representa. Només el nom i l'identificador personal són sintètics.
3. El codi de plaça es conserva perquè és la clau operativa que relaciona el
   titular amb les dades de servei i forma part de l'esquema funcional.
4. La mateixa correspondència sintètica s'aplica a descansos, històric i pla
   de referència. Això manté el patró anual i permet validar les regles.
5. Totes les dates s'han mogut 728 dies, exactament 104 setmanes. No s'aplica
   cap desplaçament individual ni cap assignació aleatòria de descansos.
6. Els motius lliures s'han eliminat o substituït per textos genèrics.
7. S'han buidat incidències, auditories, propostes, publicacions i taules
   antigues. Després s'ha reconstruït físicament SQLite amb `VACUUM`.

No es desa ni es publica cap taula de correspondència entre la base original
i la base demo.

## Controls superats

- `PRAGMA integrity_check`: correcte;
- `PRAGMA foreign_key_check`: cap incidència;
- cap treballador orfe en descansos, històric o assignacions de referència;
- cap nom original detectat en cap camp de text ni en els bytes del fitxer;
- els codis de plaça mantenen la correspondència operativa amb els serveis;
- totes les identitats compleixen el patró sintètic;
- cada persona sintètica manté el mateix patró de descansos, històric,
  rotació, zona i habilitacions de manera coherent;
- prova CP-SAT de 7 dies: 86 de 87 necessitats cobertes, exactament el mateix
  resultat que amb la base original; el descobert és per saturació
  persona-dia dels candidats compatibles;
- Planificació, Consulta i Incidències renderitzen sense errors.

## Abast i limitacions

Aquesta és una base pseudonimitzada de demostració, no una base anònima en
sentit estricte ni una base de producció. Conserva deliberadament els patrons
operatius perquè els casos de prova siguin útils. Per aquest motiu, el fitxer
s'ha de tractar igualment com un actiu intern:
només s'ha de publicar al repositori de proves previst i no s'hi ha d'afegir
mai la base original, còpies de seguretat ni fitxers que permetin comparar
ambdues bases.

Qualsevol regeneració ha de tornar a executar tots els controls amb:

```powershell
python scripts\create_demo_database.py `
  --source ..\treballadors.db `
  --output data\treballadors_demo.db `
  --replace
python scripts\verify_deploy.py --require-demo-data
```
