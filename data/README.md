# Base de demostració

La base pseudonimitzada del prototip és:

```text
treballadors_demo.db
```

S'ha generat amb `scripts/create_demo_database.py`. El procés i els controls
superats es documenten a [`ANONIMITZACIO.md`](ANONIMITZACIO.md).

No canvieu el nom del fitxer: és l'única base SQLite que `.gitignore` permet
incorporar al repositori de proves. No afegiu mai aquí la base original ni una
taula de correspondències.
