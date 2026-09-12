"""Ajuda de consulta, independent de les dades i les accions operatives."""

from pathlib import Path

import streamlit as st


GUIDE_PATH = Path(__file__).resolve().parents[2] / "docs" / "GUIA_USUARI_CP_SAT.html"

TOPICS = {
    "Resum": (
        ("Començar el dia", "1. Mireu **Requereix atenció**.\n2. Reviseu descoberts, baixes que finalitzen, incidències obertes i propostes pendents.\n3. Utilitzeu els accessos ràpids per registrar una incidència o generar una proposta."),
        ("Saber què s’ha fet", "Consulteu **Activitat recent**. Per veure les assignacions oficials i les versions, obriu **Pla publicat**."),
    ),
    "Planificació": (
        ("Generar una proposta", "1. A **Nova proposta**, escolliu les dates.\n2. Ajusteu **Opcions de l’abast** només si voleu limitar els canvis.\n3. Premeu **Revisar abast** i comproveu què queda protegit.\n4. Premeu **Generar proposta**. Encara no modifica el pla."),
        ("Validar la proposta", "1. A **Revisar propostes**, seleccioneu-la i premeu **Obrir proposta**.\n2. Reviseu **Canvis**, **Descoberts** i **Impacte i equitat**.\n3. Marqueu **Confirmo que he revisat els canvis i els descoberts**.\n4. Premeu **Validar proposta** i comproveu l’estat **Validada**.\n\nValidar encara no publica."),
        ("Publicar un bloc", "1. Obriu la proposta **Validada**.\n2. Comproveu l’inici automàtic i escolliu **Publicar fins al dia**.\n3. Marqueu la confirmació. Si és **FEASIBLE**, accepteu que no s’ha demostrat l’optimalitat.\n4. Premeu **Publicar bloc** i espereu la confirmació.\n5. Comproveu el resultat a **Pla publicat**.\n\n**Aquest pas modifica el pla.** Si apareix **Publicar canvis**, es publica l’abast revisat. En mode ombra la publicació està desactivada."),
        ("Fixar una preassignació", "1. Obriu **Preassignacions** i escolliu el període.\n2. Seleccioneu persona, serveis i dies.\n3. Premeu **Previsualitzar assignacions**.\n4. Reviseu, confirmeu i premeu **Desar preassignacions**.\n\nEl pròxim càlcul respectarà la reserva; encara no es publica. Per retirar-la, seleccioneu-la i premeu **Retirar preassignació**."),
        ("Proposta invalidada", "Reviseu el motiu i premeu **Crear nova proposta per al tram pendent**. Els blocs ja publicats es conserven."),
        ("Descartar o revertir", "**Descartar proposta:** confirmeu el descart si encara no s’ha publicat cap bloc. El pla no canvia.\n\n**Revertir últim bloc:** confirmeu la reversió només si voleu desfer aquella publicació. Es bloqueja si hi ha canvis posteriors incompatibles."),
        ("Avisos Xivato", "Si el pilot està configurat, després de publicar:\n\n1. **Preparar avisos Xivato**.\n2. **Comprovar destinataris**.\n3. Reviseu els avisos previsualitzats, confirmeu i premeu **Enviar avisos Xivato**.\n\nPublicar no envia els avisos automàticament."),
    ),
    "Pla publicat": (
        ("Consultar el pla oficial", "1. Apliqueu els filtres necessaris.\n2. Seleccioneu una assignació per veure’n la procedència, les incidències, els canvis i els bloquejos.\n3. Consulteu **Historial de versions oficials** per seguir les publicacions.\n\nAquí no apareixen les propostes pendents."),
        ("Descarregar les assignacions", "Apliqueu els filtres i premeu **Descarregar resultats (CSV)**. El fitxer conté les files filtrades."),
    ),
    "Cronograma": (
        ("Llegir el cronograma", "1. Agrupeu per zona o línia, o consulteu sense agrupació.\n2. Cerqueu o filtreu els serveis.\n3. Seleccioneu una casella per veure’n el detall.\n\nVerd: publicat. Blau: provisional. Vermell: descobert. Gris: pendent. La trama diagonal indica que el servei no circula."),
        ("Comparar amb una proposta", "A **Superposició provisional**, escolliu la proposta. Només se superposa al tram no publicat; als trams publicats preval el pla oficial.\n\nÉs un visor de consulta: no es mouen caselles ni es publiquen canvis."),
    ),
    "Hores realitzades": (
        ("Tancar un període", "1. A **Tancar període**, escolliu les dates.\n2. Premeu **Revisar el tancament**.\n3. Comproveu les assignacions pendents, les ja confirmades i les hores noves.\n4. Confirmeu i premeu **Tancar … assignacions**.\n\nFeu-ho quan hàgiu revisat la feina realitzada. Les hores ja confirmades no es tornen a sumar."),
        ("Consultar els totals", "A **Consultar i corregir**, escolliu l’any. Veureu hores i serveis confirmats, canvis de torn i zona i els percentatges. Podeu desplegar el detall i l’historial de tancaments."),
        ("Corregir un tancament", "1. Obriu **Corregir un tancament**.\n2. Seleccioneu-lo i indiqueu el motiu.\n3. Confirmeu i premeu **Revertir el tancament**.\n\nLes hores d’aquell tancament deixen de comptar; l’historial es conserva."),
    ),
    "Personal": (
        ("Consultar una persona", "A **Per treballador**, cerqueu pel nom, plaça o ID. Consulteu els descansos i moviments. **Netejar camps** reinicia la consulta."),
        ("Disponibilitat i seguiment", "Escolliu què voleu consultar: disponibilitat d’un dia, calendari mensual, serveis descoberts, estadístiques, historial o alertes de baixes.\n\nPer registrar una baixa, vacances o substitució, aneu a **Incidències**."),
    ),
    "Incidències": (
        ("Registrar la incidència", "1. A **1. Registrar**, seleccioneu la persona i el tipus.\n2. Indiqueu les dates i el motiu; en una substitució, també el substitut.\n3. Confirmeu i premeu **Registrar incidència**.\n\nEncara no modifica la disponibilitat ni el pla."),
        ("Preparar la reparació", "1. Obriu **2. Preparar reparació**.\n2. Seleccioneu la incidència oberta.\n3. Premeu **Preparar reparació CP-SAT**.\n\nEl resultat és una simulació pendent d’aprovació."),
        ("Revisar i aplicar", "1. A **3. Revisar i aplicar**, obriu la reparació.\n2. Reviseu cobertura, descoberts, abans i després i impacte sobre les persones.\n3. Confirmeu la revisió i premeu **Aprovar i aplicar reparació al pla vigent**.\n4. Comproveu el missatge de resultat.\n\n**Aquest pas modifica dades.** Una reparació vàlida pot deixar serveis descoberts. Si no cal canviar cobertures, apareix **Aplicar incidència sense canvis de cobertura**."),
        ("Eliminar un esborrany", "Desplegueu **Eliminar aquesta reparació**, confirmeu i premeu **Eliminar reparació**. La incidència queda disponible per preparar-ne una altra; el pla publicat no canvia."),
    ),
}


@st.fragment
def render_context_help(page_title: str) -> None:
    """Es crida des de la barra lateral; obrir ajuda no recalcula la pàgina."""
    if not st.toggle("Ajuda", key="mostrar_ajuda"):
        return
    st.caption(f"Ajuda · {page_title}")
    for title, body in TOPICS.get(page_title, ()):
        with st.expander(title):
            st.markdown(body)
    st.link_button("Obrir guia completa", "?ajuda=guia", icon=":material/open_in_new:")


def render_full_guide() -> None:
    """La guia és un fitxer local de l’aplicació, sense dades de sessió."""
    st.iframe(GUIDE_PATH, height=900)
