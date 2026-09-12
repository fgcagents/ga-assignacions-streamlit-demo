"""Interfície autònoma i informativa del cronograma de serveis."""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import streamlit as st

from planificador_cp_sat.services.cronograma import (
    TimelineReadError,
    filter_timeline,
    find_timeline_cell,
    load_timeline,
    load_timeline_options,
)


_ANCHOR_KEY = "cronograma_data_referencia"
_COMPONENT_KEY = "cronograma_graella"
_STATE_LABELS = {
    "published": "Publicat",
    "locked": "Publicat i bloquejat",
    "provisional": "Provisional",
    "uncovered": "Descobert",
    "provisional_uncovered": "Descobert provisional",
    "pending": "Pendent de planificar",
}
_PROPOSAL_LABELS = {
    "esborrany": "Esborrany",
    "validada": "Validada",
}


_TIMELINE_HTML = """
<div id="timeline-root" aria-live="polite"></div>
"""


_TIMELINE_CSS = """
:host {
  color: var(--st-text-color);
  font-family: var(--st-font);
}

* { box-sizing: border-box; }

.timeline-shell {
  border: 1px solid var(--st-border-color);
  border-radius: var(--st-base-radius);
  background: var(--st-background-color);
  max-height: 760px;
  overflow: auto;
  position: relative;
}

.timeline-grid {
  display: grid;
  min-width: max-content;
}

.corner, .day-header, .service-cell, .timeline-cell {
  border-bottom: 1px solid var(--st-border-color-light);
  border-right: 1px solid var(--st-border-color-light);
}

.corner, .day-header {
  align-items: center;
  background: var(--st-dataframe-header-background-color, var(--st-secondary-background-color));
  display: flex;
  min-height: 58px;
  padding: 8px 10px;
  position: sticky;
  top: 0;
  z-index: 3;
}

.corner {
  font-weight: 650;
  left: 0;
  z-index: 5;
}

.day-header {
  flex-direction: column;
  justify-content: center;
  text-align: center;
}

.day-header .weekday { font-size: 0.76rem; opacity: 0.72; }
.day-header .day { font-size: 0.95rem; font-weight: 650; }
.day-header.weekend { background: var(--st-gray-background-color); }
.day-header.today { box-shadow: inset 0 3px 0 var(--st-primary-color); }

.group-row {
  background: var(--st-secondary-background-color);
  border-bottom: 1px solid var(--st-border-color);
  min-height: 35px;
  padding: 5px 10px;
  position: sticky;
  left: 0;
  z-index: 2;
}

.group-button {
  background: transparent;
  border: 0;
  color: var(--st-text-color);
  cursor: pointer;
  font: inherit;
  font-size: 0.84rem;
  font-weight: 650;
  padding: 2px 4px;
}

.service-cell {
  background: var(--st-background-color);
  left: 0;
  min-height: 58px;
  padding: 8px 10px;
  position: sticky;
  z-index: 2;
}

.service-id { font-size: 0.91rem; font-weight: 650; }
.service-meta { font-size: 0.73rem; margin-top: 3px; opacity: 0.68; }

.timeline-cell {
  align-items: stretch;
  background: var(--st-background-color);
  display: flex;
  min-height: 58px;
  padding: 5px;
}

.timeline-cell.weekend { background: color-mix(in srgb, var(--st-gray-background-color) 35%, transparent); }
.timeline-cell.today { box-shadow: inset 2px 0 0 var(--st-primary-color), inset -2px 0 0 var(--st-primary-color); }

.pill {
  border: 1px solid transparent;
  border-radius: var(--st-button-radius);
  cursor: pointer;
  display: flex;
  flex: 1;
  flex-direction: column;
  justify-content: center;
  min-width: 0;
  padding: 6px 7px;
  text-align: left;
}

.pill:hover, .pill:focus-visible { filter: brightness(0.97); outline: 2px solid var(--st-primary-color); }
.pill.selected { outline: 2px solid var(--st-primary-color); }
.pill-name { font-size: 0.78rem; font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pill-meta { font-size: 0.68rem; margin-top: 2px; opacity: 0.78; }
.pill-mark { font-size: 0.68rem; font-weight: 700; margin-left: 4px; }

.published, .locked {
  background: var(--st-green-background-color);
  border-color: var(--st-green-color);
  color: var(--st-green-text-color, var(--st-text-color));
}

.provisional {
  background: var(--st-blue-background-color);
  border-color: var(--st-blue-color);
  color: var(--st-blue-text-color, var(--st-text-color));
}

.uncovered, .provisional_uncovered {
  background: var(--st-red-background-color);
  border-color: var(--st-red-color);
  color: var(--st-red-text-color, var(--st-text-color));
}

.pending {
  background: var(--st-gray-background-color);
  border-color: var(--st-gray-color);
  border-style: dashed;
  color: var(--st-gray-text-color, var(--st-text-color));
}

.no-service {
  background: repeating-linear-gradient(
    -45deg,
    transparent,
    transparent 6px,
    var(--st-gray-background-color) 6px,
    var(--st-gray-background-color) 7px
  );
  flex: 1;
  opacity: 0.45;
}

.empty-message { color: var(--st-gray-text-color, var(--st-text-color)); padding: 24px; text-align: center; }
"""


_TIMELINE_JS = r"""
const componentViews = new WeakMap()

const WEEKDAYS = ["Dg.", "Dl.", "Dt.", "Dc.", "Dj.", "Dv.", "Ds."]
const MONTHS = ["gen.", "febr.", "març", "abr.", "maig", "juny", "jul.", "ag.", "set.", "oct.", "nov.", "des."]
const STATUS_LABELS = {
  published: "Publicat",
  locked: "Publicat i bloquejat",
  provisional: "Provisional",
  uncovered: "Descobert",
  provisional_uncovered: "Descobert provisional",
  pending: "Pendent de planificar",
}

function dateParts(value) {
  const current = new Date(`${value}T12:00:00`)
  return {
    current,
    weekday: WEEKDAYS[current.getDay()],
    label: `${current.getDate()} ${MONTHS[current.getMonth()]}`,
    weekend: current.getDay() === 0 || current.getDay() === 6,
  }
}

function shortName(value, compact) {
  const parts = String(value || "").trim().split(/\s+/).filter(Boolean)
  if (!parts.length) return ""
  if (compact) return parts.slice(0, 2).map(part => part[0]).join("").toUpperCase()
  if (parts.length === 1) return parts[0]
  return `${parts[0]} ${parts[1][0]}.`
}

function groupLabel(row, groupBy) {
  if (groupBy === "zone") return row.zone || "Sense zona"
  if (groupBy === "line") return row.line || "Sense línia"
  return "Serveis"
}

function cellTitle(cell) {
  const details = [
    `${cell.service_id} · ${cell.date}`,
    STATUS_LABELS[cell.status] || cell.status,
  ]
  if (cell.worker_name) details.push(cell.worker_name)
  if (cell.start_time) details.push(`${cell.start_time}–${cell.end_time}${cell.overnight ? " (+1)" : ""}`)
  if (cell.line || cell.zone) details.push([cell.line, cell.zone].filter(Boolean).join(" · "))
  if (cell.incident_count) details.push(`${cell.incident_count} incidència/es`)
  if (cell.reason) details.push(cell.reason)
  return details.join("\n")
}

export default function (component) {
  const { parentElement, data, setStateValue } = component
  const root = parentElement.querySelector("#timeline-root")
  if (!root) return

  let view = componentViews.get(parentElement)
  if (!view) {
    view = { collapsed: new Set(), scrollLeft: 0, scrollTop: 0 }
    componentViews.set(parentElement, view)
  }

  const render = () => {
    const oldShell = root.querySelector(".timeline-shell")
    if (oldShell) {
      view.scrollLeft = oldShell.scrollLeft
      view.scrollTop = oldShell.scrollTop
    }

    if (!data.rows?.length) {
      const empty = document.createElement("div")
      empty.className = "empty-message"
      empty.textContent = "No hi ha serveis que coincideixin amb els filtres."
      root.replaceChildren(empty)
      return
    }

    const shell = document.createElement("div")
    shell.className = "timeline-shell"
    shell.addEventListener("scroll", () => {
      view.scrollLeft = shell.scrollLeft
      view.scrollTop = shell.scrollTop
    }, { passive: true })

    const grid = document.createElement("div")
    grid.className = "timeline-grid"
    const dayWidth = data.compact ? "92px" : "132px"
    grid.style.gridTemplateColumns = `230px repeat(${data.days.length}, ${dayWidth})`

    const corner = document.createElement("div")
    corner.className = "corner"
    corner.textContent = "Servei"
    grid.appendChild(corner)

    for (const day of data.days) {
      const parts = dateParts(day)
      const header = document.createElement("div")
      header.className = `day-header${parts.weekend ? " weekend" : ""}${day === data.today ? " today" : ""}`
      const weekday = document.createElement("span")
      weekday.className = "weekday"
      weekday.textContent = parts.weekday
      const label = document.createElement("span")
      label.className = "day"
      label.textContent = parts.label
      header.append(weekday, label)
      grid.appendChild(header)
    }

    const groups = new Map()
    for (const row of data.rows) {
      const label = groupLabel(row, data.group_by)
      if (!groups.has(label)) groups.set(label, [])
      groups.get(label).push(row)
    }

    for (const [label, rows] of groups) {
      if (data.group_by !== "none") {
        const group = document.createElement("div")
        group.className = "group-row"
        group.style.gridColumn = "1 / -1"
        const toggle = document.createElement("button")
        toggle.type = "button"
        toggle.className = "group-button"
        toggle.textContent = `${view.collapsed.has(label) ? "▸" : "▾"} ${label} · ${rows.length}`
        toggle.setAttribute("aria-expanded", String(!view.collapsed.has(label)))
        toggle.addEventListener("click", () => {
          if (view.collapsed.has(label)) view.collapsed.delete(label)
          else view.collapsed.add(label)
          render()
        })
        group.appendChild(toggle)
        grid.appendChild(group)
      }
      if (view.collapsed.has(label)) continue

      for (const row of rows) {
        const service = document.createElement("div")
        service.className = "service-cell"
        const serviceId = document.createElement("div")
        serviceId.className = "service-id"
        serviceId.textContent = row.service_id
        const meta = document.createElement("div")
        meta.className = "service-meta"
        meta.textContent = [row.line, row.zone].filter(Boolean).join(" · ") || "Sense classificació"
        service.append(serviceId, meta)
        grid.appendChild(service)

        for (const day of data.days) {
          const parts = dateParts(day)
          const holder = document.createElement("div")
          holder.className = `timeline-cell${parts.weekend ? " weekend" : ""}${day === data.today ? " today" : ""}`
          const cell = row.cells?.[day]
          if (!cell) {
            const empty = document.createElement("div")
            empty.className = "no-service"
            empty.title = "El servei no circula aquest dia"
            holder.appendChild(empty)
            grid.appendChild(holder)
            continue
          }

          const pill = document.createElement("button")
          pill.type = "button"
          const isSelected = data.selected?.date === day && data.selected?.service_id === row.service_id
          pill.className = `pill ${cell.status}${isSelected ? " selected" : ""}`
          pill.title = cellTitle(cell)
          pill.setAttribute("aria-label", cellTitle(cell).replaceAll("\n", ". "))

          const name = document.createElement("span")
          name.className = "pill-name"
          name.textContent = cell.worker_id
            ? shortName(cell.worker_id, data.compact)
            : cell.status === "pending"
            ? "Pendent"
            : "Descobert"
          if (cell.status === "locked" || cell.incident_count || cell.zone_change || cell.turn_change) {
            const mark = document.createElement("span")
            mark.className = "pill-mark"
            mark.textContent = cell.status === "locked" ? "BLOQ." : "AVÍS"
            name.appendChild(mark)
          }
          pill.appendChild(name)

          if (!data.compact && cell.start_time) {
            const detail = document.createElement("span")
            detail.className = "pill-meta"
            detail.textContent = `${cell.start_time}–${cell.end_time}${cell.overnight ? " (+1)" : ""}`
            pill.appendChild(detail)
          }
          pill.addEventListener("click", () => {
            setStateValue("selected", { date: day, service_id: row.service_id })
          })
          holder.appendChild(pill)
          grid.appendChild(holder)
        }
      }
    }

    shell.appendChild(grid)
    root.replaceChildren(shell)
    requestAnimationFrame(() => {
      shell.scrollLeft = view.scrollLeft
      shell.scrollTop = view.scrollTop
    })
  }

  render()
}
"""


_TIMELINE_COMPONENT = st.components.v2.component(
    "cronograma_serveis",
    html=_TIMELINE_HTML,
    css=_TIMELINE_CSS,
    js=_TIMELINE_JS,
)


@st.cache_data(ttl=30, max_entries=8, show_spinner=False)
def _cached_options(database_path: str) -> dict[str, Any]:
    return load_timeline_options(database_path)


@st.cache_data(ttl=15, max_entries=64, show_spinner=False)
def _cached_timeline(
    database_path: str,
    start_date: date,
    end_date: date,
    proposal_id: int | None,
) -> dict[str, Any]:
    return load_timeline(
        database_path,
        start_date,
        end_date,
        proposal_id=proposal_id,
    )


def _shift_month(value: date, offset: int) -> date:
    month_index = value.year * 12 + value.month - 1 + offset
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _period(anchor: date, view: str) -> tuple[date, date]:
    if view == "Mes":
        start = anchor.replace(day=1)
        return start, start.replace(day=calendar.monthrange(start.year, start.month)[1])
    start = anchor - timedelta(days=anchor.weekday())
    return start, start + timedelta(days=6)


def _proposal_label(proposal: dict[str, Any]) -> str:
    state = _PROPOSAL_LABELS.get(proposal["estat"], proposal["estat"])
    return (
        f"P-{proposal['id']} · {state} · "
        f"{proposal['data_inici']} – {proposal['data_fi']}"
    )


def _render_navigation(options: dict[str, Any], view: str) -> tuple[date, date]:
    default_anchor = options["official_end"] or options["coverage_end"]
    st.session_state.setdefault(_ANCHOR_KEY, default_anchor)
    current = st.session_state[_ANCHOR_KEY]
    previous, picker, following, today_column, latest = st.columns(
        [0.7, 2.2, 0.7, 1, 1.25]
    )
    if previous.button(
        "Anterior", icon=":material/chevron_left:", width="stretch"
    ):
        st.session_state[_ANCHOR_KEY] = (
            _shift_month(current, -1)
            if view == "Mes"
            else current - timedelta(days=7)
        )
        st.rerun()
    selected = picker.date_input(
        "Tria una data",
        min_value=options["coverage_start"],
        max_value=options["coverage_end"],
        format="DD/MM/YYYY",
        key=_ANCHOR_KEY,
        label_visibility="collapsed",
    )
    if following.button(
        "Següent", icon=":material/chevron_right:", width="stretch"
    ):
        st.session_state[_ANCHOR_KEY] = (
            _shift_month(selected, 1)
            if view == "Mes"
            else selected + timedelta(days=7)
        )
        st.rerun()
    today = date.today()
    today_available = options["coverage_start"] <= today <= options["coverage_end"]
    if today_column.button(
        "Avui",
        icon=":material/today:",
        width="stretch",
        disabled=not today_available,
    ):
        st.session_state[_ANCHOR_KEY] = today
        st.rerun()
    if latest.button(
        "Darrer pla",
        icon=":material/update:",
        width="stretch",
        disabled=options["official_end"] is None,
    ):
        st.session_state[_ANCHOR_KEY] = options["official_end"]
        st.rerun()
    return _period(selected, view)


def _render_filters(
    options: dict[str, Any],
) -> tuple[dict[str, Any], str, int | None]:
    with st.sidebar:
        st.subheader("Vista")
        group_label = st.segmented_control(
            "Agrupar per",
            ["Zona", "Línia", "Sense agrupació"],
            default="Zona",
            key="cronograma_agrupacio",
        )
        proposals = {int(item["id"]): item for item in options["proposals"]}
        proposal_id = st.selectbox(
            "Superposició provisional",
            options=[None, *proposals],
            format_func=lambda value: (
                "Només pla oficial"
                if value is None
                else _proposal_label(proposals[value])
            ),
            key="cronograma_proposta",
        )
        st.divider()
        st.subheader("Filtres")
        search = st.text_input(
            "Cerca servei o treballador",
            icon=":material/search:",
            key="cronograma_cerca",
        )
        lines = st.multiselect(
            "Línia", options["lines"], key="cronograma_linies"
        )
        zones = st.multiselect(
            "Zona", options["zones"], key="cronograma_zones"
        )
        services = st.multiselect(
            "Servei", options["services"], key="cronograma_serveis"
        )
        worker_labels = {
            item["id"]: f"{item['id']} · {item['name']}"
            for item in options["workers"]
        }
        workers = st.multiselect(
            "Treballador",
            options=list(worker_labels),
            format_func=lambda value: worker_labels[value],
            key="cronograma_treballadors",
        )
        status_labels = {value: label for value, label in _STATE_LABELS.items()}
        statuses = st.multiselect(
            "Estat",
            options=list(status_labels),
            format_func=lambda value: status_labels[value],
            key="cronograma_estats",
        )
        if st.button(
            "Actualitzar dades",
            icon=":material/refresh:",
            width="stretch",
        ):
            st.cache_data.clear()
            st.rerun()
        st.caption("Consulta de només lectura. No modifica la planificació.")
    filters = {
        "lines": lines,
        "zones": zones,
        "services": services,
        "worker_ids": workers,
        "statuses": statuses,
        "search": search,
    }
    group_by = {
        "Zona": "zone",
        "Línia": "line",
        "Sense agrupació": "none",
    }[group_label or "Zona"]
    return filters, group_by, proposal_id


def _render_totals(totals: dict[str, int]) -> None:
    with st.container(horizontal=True):
        st.metric("Serveis-dia", totals["coverage"], border=True)
        st.metric(
            "Publicats",
            totals["published"] + totals["locked"],
            border=True,
        )
        st.metric("Provisionals", totals["provisional"], border=True)
        st.metric(
            "Descoberts",
            totals["uncovered"] + totals["provisional_uncovered"],
            border=True,
        )
        st.metric("Pendents", totals["pending"], border=True)


def _render_legend() -> None:
    with st.container(horizontal=True, gap="small"):
        st.badge("Publicat", color="green")
        st.badge("Provisional", color="blue")
        st.badge("Descobert", color="red")
        st.badge("Pendent", color="gray")
        st.caption("La trama diagonal indica que el servei no circula.")


def _render_detail(cell: dict[str, Any]) -> None:
    st.subheader("Detall de la casella")
    state_label = _STATE_LABELS.get(cell["status"], cell["status"])
    badge_color = {
        "published": "green",
        "locked": "green",
        "provisional": "blue",
        "uncovered": "red",
        "provisional_uncovered": "red",
        "pending": "gray",
    }[cell["status"]]
    st.badge(state_label, color=badge_color)
    st.markdown(
        f"**Servei {cell['service_id']} · "
        f"{datetime.fromisoformat(cell['date']).strftime('%d/%m/%Y')}**"
    )
    details = [
        f"Línia: {cell['line'] or '—'}",
        f"Zona: {cell['zone'] or '—'}",
        f"Habilitació: {cell['skills'] or '—'}",
    ]
    if cell["worker_name"]:
        details.insert(0, f"Treballador: {cell['worker_id']} · {cell['worker_name']}")
    if cell["start_time"]:
        suffix = " (+1)" if cell["overnight"] else ""
        details.append(
            f"Horari: {cell['start_time']}–{cell['end_time']}{suffix}"
        )
    if cell["proposal_id"]:
        details.append(
            f"Proposta: P-{cell['proposal_id']} · "
            f"{_PROPOSAL_LABELS.get(cell['proposal_state'], cell['proposal_state'])}"
        )
    st.write("  \n".join(details))
    if cell["previous_worker_name"]:
        st.caption(
            "Assignació anterior: "
            f"{cell['previous_worker_id']} · {cell['previous_worker_name']}"
        )
    if cell["reason"]:
        st.info(cell["reason"], icon=":material/info:")
    alerts = []
    if cell["incident_count"]:
        alerts.append(f"{cell['incident_count']} incidència/es relacionades")
    if cell["zone_change"]:
        alerts.append("assignació fora de la zona preferent")
    if cell["turn_change"]:
        alerts.append("assignació fora del torn preferent")
    if alerts:
        st.warning(" · ".join(alerts), icon=":material/warning:")


def render_cronograma(database_path: str | Path) -> None:
    """Renderitza el visor independent sense accions operatives."""
    st.header("Cronograma de serveis")
    st.caption(
        "Consulta el pla oficial i, opcionalment, una proposta concreta. "
        "Aquesta aplicació no permet editar ni publicar assignacions."
    )
    try:
        options = _cached_options(str(database_path))
        filters, group_by, proposal_id = _render_filters(options)
        view = st.segmented_control(
            "Escala temporal",
            ["Setmana", "Mes"],
            default="Setmana",
            key="cronograma_escala",
        )
        start, end = _render_navigation(options, view or "Setmana")
        st.caption(
            f"Període visible: {start.strftime('%d/%m/%Y')} – "
            f"{end.strftime('%d/%m/%Y')}"
        )
        timeline = _cached_timeline(
            str(database_path), start, end, proposal_id
        )
        visible = filter_timeline(timeline, **filters)
        proposal = visible["proposal"]
        if proposal and not proposal["applied"]:
            st.warning(proposal["message"], icon=":material/update_disabled:")
        elif proposal:
            st.info(
                f"Superposició informativa P-{proposal['id']} "
                f"({_PROPOSAL_LABELS.get(proposal['state'], proposal['state'])}).",
                icon=":material/layers:",
            )
        _render_totals(visible["totals"])
        _render_legend()

        component_state = st.session_state.get(_COMPONENT_KEY, {})
        selected = (
            component_state.get("selected")
            if hasattr(component_state, "get")
            else None
        )
        result = _TIMELINE_COMPONENT(
            key=_COMPONENT_KEY,
            data={
                "days": visible["days"],
                "rows": visible["rows"],
                "compact": view == "Mes",
                "group_by": group_by,
                "today": date.today().isoformat(),
                "selected": selected,
            },
            height=min(760, max(280, 110 + len(visible["rows"]) * 58)),
            on_selected_change=lambda: None,
        )
        cell = find_timeline_cell(visible, getattr(result, "selected", None))
        if cell:
            _render_detail(cell)
        else:
            st.caption("Selecciona una pastilla o casella per veure'n el detall.")
    except (OSError, sqlite3.Error, TimelineReadError) as error:
        st.error(str(error), icon=":material/database_off:")
