/* main.js — ReserCEEYS */

let calendar;
let salaFiltro = '';

// ─────────────────────────────────────────────────────────
// Calendario FullCalendar
// ─────────────────────────────────────────────────────────
function initCalendario(apiUrl) {
  const el = document.getElementById('calendario');
  if (!el) return;

  calendar = new FullCalendar.Calendar(el, {
    locale: 'es',
    initialView: 'dayGridMonth',
    headerToolbar: {
      left:   'prev,next today',
      center: 'title',
      right:  'dayGridMonth,timeGridWeek,timeGridDay,listWeek'
    },
    buttonText: {
      today:    'Hoy',
      month:    'Mes',
      week:     'Semana',
      day:      'Día',
      list:     'Lista'
    },
    height: 'auto',
    nowIndicator: true,
    businessHours: {
      daysOfWeek: [1, 2, 3, 4, 5],
      startTime: '07:00',
      endTime: '22:00',
    },
    events: function(info, successCallback, failureCallback) {
      let url = `${apiUrl}?start=${info.startStr}&end=${info.endStr}`;
      if (salaFiltro) url += `&sala_id=${salaFiltro}`;
      fetch(url)
        .then(r => r.json())
        .then(successCallback)
        .catch(failureCallback);
    },
    eventClick: function(info) {
      mostrarDetalleEvento(info.event);
    },
    eventDidMount: function(info) {
      info.el.setAttribute('title',
        `${info.event.extendedProps.sala} | ${info.event.title}`);
    }
  });

  calendar.render();
}

// ─────────────────────────────────────────────────────────
// Detalle de evento (panel lateral + modal en móvil)
// ─────────────────────────────────────────────────────────
function mostrarDetalleEvento(event) {
  const props = event.extendedProps;
  const inicio = formatHora(event.start);
  const fin    = event.end ? formatHora(event.end) : '';

  // Panel lateral (escritorio)
  const detalleCard = document.getElementById('detalle-card');
  const detalleBody = document.getElementById('detalle-body');
  if (detalleCard && detalleBody) {
    detalleBody.innerHTML = `
      <p class="mb-1"><strong>Sala:</strong> ${props.sala}</p>
      <p class="mb-1"><strong>Fecha:</strong> ${event.start.toLocaleDateString('es-PE')}</p>
      <p class="mb-1"><strong>Horario:</strong> ${inicio} – ${fin}</p>
      <p class="mb-1"><strong>Solicitante:</strong>
        ${props.solicitante_cargo ? `<span class="badge bg-primary me-1">${props.solicitante_cargo}</span>` : ''}
        ${props.solicitante_nombre || '—'}
      </p>
      <p class="mb-1"><strong>Registra:</strong> ${props.registra}</p>
    `;
    detalleCard.style.display = '';

    // Botones admin
    const btnEditar = document.getElementById('btn-editar');
    const formAnular = document.getElementById('form-anular');
    if (btnEditar) btnEditar.href = `/reservas/${event.id}/editar`;
    if (formAnular) formAnular.action = `/reservas/${event.id}/anular`;
  }

  // Modal (visible en todas las pantallas como alternativa)
  const modal = document.getElementById('modalEvento');
  if (modal) {
    document.getElementById('modalTitulo').textContent = event.title;
    document.getElementById('modalBody').innerHTML = `
      <div class="row g-2 small">
        <div class="col-6"><strong>Sala:</strong><br>${props.sala}</div>
        <div class="col-6"><strong>Fecha:</strong><br>${event.start.toLocaleDateString('es-PE')}</div>
        <div class="col-12"><strong>Horario:</strong> ${inicio} – ${fin}</div>
        <div class="col-12"><strong>Solicitante:</strong>
          ${props.solicitante_cargo ? `<span class="badge bg-primary me-1">${props.solicitante_cargo}</span>` : ''}
          ${props.solicitante_nombre || '—'}
        </div>
        <div class="col-12"><strong>Registra:</strong> ${props.registra}</div>
      </div>
    `;
    const footer = document.getElementById('modalFooter');
    if (footer) {
      if (typeof ES_ADMIN !== 'undefined' && ES_ADMIN) {
        footer.innerHTML = `
          <a href="/reservas/${event.id}/editar" class="btn btn-sm btn-outline-primary">
            <i class="bi bi-pencil"></i> Editar
          </a>
          <form method="POST" action="/reservas/${event.id}/anular" class="d-inline"
                onsubmit="return confirm('¿Anular esta reserva?')">
            <button type="submit" class="btn btn-sm btn-outline-danger">
              <i class="bi bi-x-circle"></i> Anular
            </button>
          </form>
          <button class="btn btn-sm btn-secondary ms-auto" data-bs-dismiss="modal">Cerrar</button>
        `;
      } else {
        footer.innerHTML = `<button class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cerrar</button>`;
      }
    }
    new bootstrap.Modal(modal).show();
  }
}

// ─────────────────────────────────────────────────────────
// Filtro por sala
// ─────────────────────────────────────────────────────────
function initFiltroSalas() {
  document.querySelectorAll('.sala-card').forEach(card => {
    card.addEventListener('click', function() {
      document.querySelectorAll('.sala-card').forEach(c => c.classList.remove('active-sala'));
      this.classList.add('active-sala');
      salaFiltro = this.dataset.sala || '';
      if (calendar) calendar.refetchEvents();
    });
  });
}

// ─────────────────────────────────────────────────────────
// Utilidades
// ─────────────────────────────────────────────────────────
function formatHora(date) {
  if (!date) return '';
  return date.toLocaleTimeString('es-PE', { hour: '2-digit', minute: '2-digit', hour12: false });
}
