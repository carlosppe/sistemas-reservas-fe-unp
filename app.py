from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash
import os
from datetime import datetime
from functools import wraps

# ── PostgreSQL ─────────────────────────────────────────────
import psycopg2
import psycopg2.extras

app = Flask(__name__)
# SECRET_KEY debe estar en las variables de entorno de Render
app.secret_key = os.environ.get('SECRET_KEY', 'dev-local-secret-cambia-en-produccion')

# ---------------------------------------------------------------------------
# Utilidades de base de datos
# ---------------------------------------------------------------------------

def _dsn():
    """Normaliza la URL de conexión (Render entrega postgres://, psycopg2 necesita postgresql://)."""
    dsn = os.environ.get('DATABASE_URL', '')
    if dsn.startswith('postgres://'):
        dsn = dsn.replace('postgres://', 'postgresql://', 1)
    return dsn


class _DbWrapper:
    """Envuelve psycopg2 con una API similar a sqlite3 para compatibilidad."""
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or [])
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self._conn.rollback()
        self._conn.close()


def get_db():
    conn = psycopg2.connect(_dsn(), sslmode='require')
    return _DbWrapper(conn)


def init_db():
    conn = get_db()

    conn.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id        SERIAL PRIMARY KEY,
            nombre    TEXT NOT NULL,
            email     TEXT UNIQUE NOT NULL,
            password  TEXT NOT NULL,
            rol       TEXT NOT NULL DEFAULT 'usuario'
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS salas (
            id          SERIAL PRIMARY KEY,
            nombre      TEXT NOT NULL,
            descripcion TEXT,
            capacidad   INTEGER,
            piso        TEXT
        )
    ''')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS reservas (
            id                 SERIAL PRIMARY KEY,
            sala_id            INTEGER NOT NULL REFERENCES salas(id),
            usuario_id         INTEGER NOT NULL REFERENCES usuarios(id),
            titulo             TEXT NOT NULL,
            fecha              DATE NOT NULL,
            hora_inicio        VARCHAR(5) NOT NULL,
            hora_fin           VARCHAR(5) NOT NULL,
            solicitante_cargo  TEXT NOT NULL DEFAULT '',
            solicitante_nombre TEXT NOT NULL DEFAULT '',
            estado             TEXT NOT NULL DEFAULT 'activa',
            creado_en          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Migración: agregar columnas si no existen
    for col, definition in [
        ('solicitante_cargo',  "TEXT NOT NULL DEFAULT ''"),
        ('solicitante_nombre', "TEXT NOT NULL DEFAULT ''"),
    ]:
        conn.execute(f'ALTER TABLE reservas ADD COLUMN IF NOT EXISTS {col} {definition}')

    # Datos iniciales: admin
    admin = conn.execute(
        "SELECT id FROM usuarios WHERE email=%s", ('admin@unp.edu.pe',)
    ).fetchone()
    if not admin:
        conn.execute(
            "INSERT INTO usuarios (nombre, email, password, rol) VALUES (%s,%s,%s,%s)",
            ('Administrador', 'admin@unp.edu.pe', generate_password_hash('admin123'), 'admin')
        )

    # Salas predefinidas
    count = conn.execute("SELECT COUNT(*) FROM salas").fetchone()['count']
    if count == 0:
        salas = [
            ('Auditorio',          'Auditorio principal de la facultad',              200, 'Planta baja'),
            ('Sala de Cómputo 01', 'Laboratorio de cómputo con 40 PCs',               40,  'Segundo piso'),
            ('Sala de Cómputo 02', 'Laboratorio de cómputo con 20 PCs',               20,  'Tercer piso'),
            ('Sala de Reuniones 01','Sala de reuniones para docentes y personal',     20,  'Segundo piso'),
            ('Sala de Reuniones 02','Sala de reuniones para docentes y personal',     20,  'Tercer piso'),
        ]
        for s in salas:
            conn.execute(
                "INSERT INTO salas (nombre, descripcion, capacidad, piso) VALUES (%s,%s,%s,%s)", s
            )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Decoradores de protección
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('rol') != 'admin':
            flash('Acceso restringido a administradores.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Rutas de autenticación
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        conn = get_db()
        user = conn.execute("SELECT * FROM usuarios WHERE email=%s", (email,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['nombre']   = user['nombre']
            session['rol']      = user['rol']
            return redirect(url_for('dashboard'))
        flash('Credenciales incorrectas.', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ---------------------------------------------------------------------------
# Dashboard principal
# ---------------------------------------------------------------------------

@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    salas = conn.execute("SELECT * FROM salas").fetchall()
    hoy = datetime.today().date()
    reservas_hoy = conn.execute(
        '''SELECT r.*, s.nombre AS sala_nombre, u.nombre AS usuario_nombre
           FROM reservas r
           JOIN salas s ON r.sala_id = s.id
           JOIN usuarios u ON r.usuario_id = u.id
           WHERE r.fecha = %s AND r.estado = 'activa'
           ORDER BY r.hora_inicio''',
        (str(hoy),)
    ).fetchall()
    conn.close()
    return render_template('dashboard.html', salas=salas, reservas_hoy=reservas_hoy, hoy=hoy)


# ---------------------------------------------------------------------------
# API de reservas (para el calendario)
# ---------------------------------------------------------------------------

@app.route('/api/reservas')
@login_required
def api_reservas():
    start   = request.args.get('start', '')
    end     = request.args.get('end', '')
    sala_id = request.args.get('sala_id', '')
    conn    = get_db()
    query   = '''
        SELECT r.id, r.titulo, r.fecha, r.hora_inicio, r.hora_fin, r.estado,
               r.solicitante_cargo, r.solicitante_nombre,
               s.nombre AS sala_nombre, u.nombre AS usuario_nombre, r.sala_id
        FROM reservas r
        JOIN salas s ON r.sala_id = s.id
        JOIN usuarios u ON r.usuario_id = u.id
        WHERE r.estado = 'activa'
    '''
    params = []
    if start:
        query += " AND r.fecha >= %s"
        params.append(start[:10])
    if end:
        query += " AND r.fecha <= %s"
        params.append(end[:10])
    if sala_id:
        query += " AND r.sala_id = %s"
        params.append(sala_id)
    reservas = conn.execute(query, params).fetchall()
    conn.close()

    colores = {1: '#1565C0', 2: '#2E7D32', 3: '#6A1B9A', 4: '#E65100', 5: '#00838F'}
    eventos = []
    for r in reservas:
        # psycopg2 devuelve DATE como datetime.date → convertir a string
        fecha_str = str(r['fecha'])
        eventos.append({
            'id':    r['id'],
            'title': f"{r['titulo']} — {r['sala_nombre']}",
            'start': f"{fecha_str}T{r['hora_inicio']}",
            'end':   f"{fecha_str}T{r['hora_fin']}",
            'color': colores.get(r['sala_id'], '#1565C0'),
            'extendedProps': {
                'sala':              r['sala_nombre'],
                'registra':          r['usuario_nombre'],
                'sala_id':           r['sala_id'],
                'solicitante_cargo': r['solicitante_cargo'],
                'solicitante_nombre':r['solicitante_nombre'],
            }
        })
    return jsonify(eventos)


@app.route('/api/reservas/<int:reserva_id>')
@login_required
def api_reserva_detalle(reserva_id):
    conn = get_db()
    r = conn.execute(
        '''SELECT r.*, s.nombre AS sala_nombre, u.nombre AS usuario_nombre
           FROM reservas r
           JOIN salas s ON r.sala_id = s.id
           JOIN usuarios u ON r.usuario_id = u.id
           WHERE r.id = %s''',
        (reserva_id,)
    ).fetchone()
    conn.close()
    if not r:
        return jsonify({'error': 'No encontrado'}), 404
    return jsonify({k: str(v) if hasattr(v, 'isoformat') else v for k, v in dict(r).items()})


# ---------------------------------------------------------------------------
# CRUD de reservas (solo admin)
# ---------------------------------------------------------------------------

@app.route('/reservas/nueva', methods=['GET', 'POST'])
@admin_required
def nueva_reserva():
    conn  = get_db()
    salas = conn.execute("SELECT * FROM salas").fetchall()
    if request.method == 'POST':
        sala_id            = request.form.get('sala_id')
        titulo             = request.form.get('titulo', '').strip()
        fecha              = request.form.get('fecha')
        hora_inicio        = request.form.get('hora_inicio')
        hora_fin           = request.form.get('hora_fin')
        solicitante_cargo  = request.form.get('solicitante_cargo', '').strip()
        solicitante_nombre = request.form.get('solicitante_nombre', '').strip()

        conflicto = conn.execute(
            '''SELECT id FROM reservas
               WHERE sala_id=%s AND fecha=%s AND estado='activa'
               AND NOT (hora_fin <= %s OR hora_inicio >= %s)''',
            (sala_id, fecha, hora_inicio, hora_fin)
        ).fetchone()
        if conflicto:
            flash('Conflicto de horario: la sala ya tiene una reserva en ese rango.', 'danger')
        elif hora_inicio >= hora_fin:
            flash('La hora de inicio debe ser anterior a la hora de fin.', 'danger')
        else:
            conn.execute(
                '''INSERT INTO reservas
                   (sala_id, usuario_id, titulo, fecha, hora_inicio, hora_fin,
                    solicitante_cargo, solicitante_nombre)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                (sala_id, session['user_id'], titulo, fecha,
                 hora_inicio, hora_fin, solicitante_cargo, solicitante_nombre)
            )
            conn.commit()
            flash('Reserva creada exitosamente.', 'success')
            conn.close()
            return redirect(url_for('historial'))
    conn.close()
    return render_template('nueva_reserva.html', salas=salas)


@app.route('/reservas/<int:reserva_id>/editar', methods=['GET', 'POST'])
@admin_required
def editar_reserva(reserva_id):
    conn    = get_db()
    reserva = conn.execute("SELECT * FROM reservas WHERE id=%s", (reserva_id,)).fetchone()
    salas   = conn.execute("SELECT * FROM salas").fetchall()
    if not reserva:
        conn.close()
        flash('Reserva no encontrada.', 'danger')
        return redirect(url_for('historial'))

    if request.method == 'POST':
        sala_id            = request.form.get('sala_id')
        titulo             = request.form.get('titulo', '').strip()
        fecha              = request.form.get('fecha')
        hora_inicio        = request.form.get('hora_inicio')
        hora_fin           = request.form.get('hora_fin')
        solicitante_cargo  = request.form.get('solicitante_cargo', '').strip()
        solicitante_nombre = request.form.get('solicitante_nombre', '').strip()

        conflicto = conn.execute(
            '''SELECT id FROM reservas
               WHERE sala_id=%s AND fecha=%s AND estado='activa' AND id!=%s
               AND NOT (hora_fin <= %s OR hora_inicio >= %s)''',
            (sala_id, fecha, reserva_id, hora_inicio, hora_fin)
        ).fetchone()
        if conflicto:
            flash('Conflicto de horario: la sala ya tiene una reserva en ese rango.', 'danger')
        elif hora_inicio >= hora_fin:
            flash('La hora de inicio debe ser anterior a la hora de fin.', 'danger')
        else:
            conn.execute(
                '''UPDATE reservas
                   SET sala_id=%s, titulo=%s, fecha=%s, hora_inicio=%s, hora_fin=%s,
                       solicitante_cargo=%s, solicitante_nombre=%s
                   WHERE id=%s''',
                (sala_id, titulo, fecha, hora_inicio, hora_fin,
                 solicitante_cargo, solicitante_nombre, reserva_id)
            )
            conn.commit()
            flash('Reserva actualizada.', 'success')
            conn.close()
            return redirect(url_for('historial'))
    conn.close()
    return render_template('editar_reserva.html', reserva=reserva, salas=salas)


@app.route('/reservas/<int:reserva_id>/anular', methods=['POST'])
@admin_required
def anular_reserva(reserva_id):
    conn = get_db()
    conn.execute("UPDATE reservas SET estado='anulada' WHERE id=%s", (reserva_id,))
    conn.commit()
    conn.close()
    flash('Reserva anulada.', 'warning')
    return redirect(url_for('historial'))


# ---------------------------------------------------------------------------
# Historial
# ---------------------------------------------------------------------------

@app.route('/historial')
@login_required
def historial():
    conn    = get_db()
    salas   = conn.execute("SELECT * FROM salas").fetchall()
    sala_id = request.args.get('sala_id', '')
    estado  = request.args.get('estado', '')
    query   = '''
        SELECT r.*, s.nombre AS sala_nombre, u.nombre AS usuario_nombre
        FROM reservas r
        JOIN salas s ON r.sala_id = s.id
        JOIN usuarios u ON r.usuario_id = u.id
        WHERE 1=1
    '''
    params = []
    if sala_id:
        query += " AND r.sala_id=%s"
        params.append(sala_id)
    if estado:
        query += " AND r.estado=%s"
        params.append(estado)
    query += " ORDER BY r.fecha DESC, r.hora_inicio DESC"
    reservas = conn.execute(query, params).fetchall()
    conn.close()
    return render_template('historial.html', reservas=reservas, salas=salas,
                           filtro_sala=sala_id, filtro_estado=estado)


# ---------------------------------------------------------------------------
# Gestión de usuarios (solo admin)
# ---------------------------------------------------------------------------

@app.route('/admin/usuarios')
@admin_required
def admin_usuarios():
    conn     = get_db()
    usuarios = conn.execute(
        "SELECT id, nombre, email, rol FROM usuarios ORDER BY nombre"
    ).fetchall()
    conn.close()
    return render_template('admin_usuarios.html', usuarios=usuarios)


@app.route('/admin/usuarios/nuevo', methods=['GET', 'POST'])
@admin_required
def admin_nuevo_usuario():
    if request.method == 'POST':
        nombre   = request.form.get('nombre', '').strip()
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        rol      = request.form.get('rol', 'usuario')
        conn     = get_db()
        existente = conn.execute(
            "SELECT id FROM usuarios WHERE email=%s", (email,)
        ).fetchone()
        if existente:
            flash('El correo ya está registrado.', 'danger')
        else:
            conn.execute(
                "INSERT INTO usuarios (nombre, email, password, rol) VALUES (%s,%s,%s,%s)",
                (nombre, email, generate_password_hash(password), rol)
            )
            conn.commit()
            flash('Usuario creado exitosamente.', 'success')
            conn.close()
            return redirect(url_for('admin_usuarios'))
        conn.close()
    return render_template('admin_nuevo_usuario.html')


@app.route('/admin/usuarios/<int:uid>/eliminar', methods=['POST'])
@admin_required
def admin_eliminar_usuario(uid):
    if uid == session['user_id']:
        flash('No puedes eliminar tu propia cuenta.', 'danger')
        return redirect(url_for('admin_usuarios'))
    conn = get_db()
    conn.execute("DELETE FROM usuarios WHERE id=%s", (uid,))
    conn.commit()
    conn.close()
    flash('Usuario eliminado.', 'warning')
    return redirect(url_for('admin_usuarios'))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000, use_reloader=False)


app = Flask(__name__)
app.secret_key = os.urandom(24)

DB_PATH = os.path.join(os.path.dirname(__file__), 'reservas.db')

# ---------------------------------------------------------------------------
# Utilidades de base de datos
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            rol TEXT NOT NULL DEFAULT 'usuario'
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS salas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            descripcion TEXT,
            capacidad INTEGER,
            piso TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS reservas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sala_id INTEGER NOT NULL,
            usuario_id INTEGER NOT NULL,
            titulo TEXT NOT NULL,
            fecha DATE NOT NULL,
            hora_inicio TEXT NOT NULL,
            hora_fin TEXT NOT NULL,
            solicitante_cargo TEXT NOT NULL DEFAULT '',
            solicitante_nombre TEXT NOT NULL DEFAULT '',
            estado TEXT NOT NULL DEFAULT 'activa',
            creado_en DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (sala_id) REFERENCES salas(id),
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
        )
    ''')

    # Migración: agregar columnas si no existen (BD ya creada)
    for col, definition in [
        ('solicitante_cargo', "TEXT NOT NULL DEFAULT ''"),
        ('solicitante_nombre', "TEXT NOT NULL DEFAULT ''"),
    ]:
        try:
            c.execute(f'ALTER TABLE reservas ADD COLUMN {col} {definition}')
        except Exception:
            pass

    # Datos iniciales: admin
    admin = c.execute("SELECT id FROM usuarios WHERE email='admin@unp.edu.pe'").fetchone()
    if not admin:
        c.execute(
            "INSERT INTO usuarios (nombre, email, password, rol) VALUES (?,?,?,?)",
            ('Administrador', 'admin@unp.edu.pe', generate_password_hash('admin123'), 'admin')
        )

    # Salas predefinidas
    if c.execute("SELECT COUNT(*) FROM salas").fetchone()[0] == 0:
        salas = [
            ('Auditorio', 'Auditorio principal de la facultad', 200, 'Planta baja'),
            ('Sala de Cómputo 01', 'Laboratorio de cómputo con 40 PCs', 40, 'Segundo piso'),
            ('Sala de Cómputo 02', 'Laboratorio de cómputo con 20 PCs', 20, 'Tercer piso'),
            ('Sala de Reuniones 01', 'Sala de reuniones para docentes y personal', 20, 'Segundo piso'),
            ('Sala de Reuniones 02', 'Sala de reuniones para docentes y personal', 20, 'Tercer piso'),
        ]
        c.executemany(
            "INSERT INTO salas (nombre, descripcion, capacidad, piso) VALUES (?,?,?,?)",
            salas
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Decoradores de protección
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('rol') != 'admin':
            flash('Acceso restringido a administradores.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Rutas de autenticación
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        conn = get_db()
        user = conn.execute("SELECT * FROM usuarios WHERE email=?", (email,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['nombre'] = user['nombre']
            session['rol'] = user['rol']
            return redirect(url_for('dashboard'))
        flash('Credenciales incorrectas.', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ---------------------------------------------------------------------------
# Dashboard principal
# ---------------------------------------------------------------------------

@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db()
    salas = conn.execute("SELECT * FROM salas").fetchall()
    hoy = datetime.today().date()
    reservas_hoy = conn.execute(
        '''SELECT r.*, s.nombre AS sala_nombre, u.nombre AS usuario_nombre
           FROM reservas r
           JOIN salas s ON r.sala_id = s.id
           JOIN usuarios u ON r.usuario_id = u.id
           WHERE r.fecha = ? AND r.estado = 'activa'
           ORDER BY r.hora_inicio''',
        (str(hoy),)
    ).fetchall()
    conn.close()
    return render_template('dashboard.html', salas=salas, reservas_hoy=reservas_hoy, hoy=hoy)


# ---------------------------------------------------------------------------
# API de reservas (para el calendario)
# ---------------------------------------------------------------------------

@app.route('/api/reservas')
@login_required
def api_reservas():
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    sala_id = request.args.get('sala_id', '')
    conn = get_db()
    query = '''
        SELECT r.id, r.titulo, r.fecha, r.hora_inicio, r.hora_fin, r.estado,
               r.solicitante_cargo, r.solicitante_nombre,
               s.nombre AS sala_nombre, u.nombre AS usuario_nombre, r.sala_id
        FROM reservas r
        JOIN salas s ON r.sala_id = s.id
        JOIN usuarios u ON r.usuario_id = u.id
        WHERE r.estado = 'activa'
    '''
    params = []
    if start:
        query += " AND r.fecha >= ?"
        params.append(start[:10])
    if end:
        query += " AND r.fecha <= ?"
        params.append(end[:10])
    if sala_id:
        query += " AND r.sala_id = ?"
        params.append(sala_id)
    reservas = conn.execute(query, params).fetchall()
    conn.close()

    eventos = []
    colores = {1: '#1565C0', 2: '#2E7D32', 3: '#6A1B9A', 4: '#E65100', 5: '#00838F'}
    for r in reservas:
        eventos.append({
            'id': r['id'],
            'title': f"{r['titulo']} — {r['sala_nombre']}",
            'start': f"{r['fecha']}T{r['hora_inicio']}",
            'end': f"{r['fecha']}T{r['hora_fin']}",
            'color': colores.get(r['sala_id'], '#1565C0'),
            'extendedProps': {
                'sala': r['sala_nombre'],
                'registra': r['usuario_nombre'],
                'sala_id': r['sala_id'],
                'solicitante_cargo': r['solicitante_cargo'],
                'solicitante_nombre': r['solicitante_nombre'],
            }
        })
    return jsonify(eventos)


@app.route('/api/reservas/<int:reserva_id>')
@login_required
def api_reserva_detalle(reserva_id):
    conn = get_db()
    r = conn.execute(
        '''SELECT r.*, s.nombre AS sala_nombre, u.nombre AS usuario_nombre
           FROM reservas r
           JOIN salas s ON r.sala_id = s.id
           JOIN usuarios u ON r.usuario_id = u.id
           WHERE r.id = ?''',
        (reserva_id,)
    ).fetchone()
    conn.close()
    if not r:
        return jsonify({'error': 'No encontrado'}), 404
    return jsonify(dict(r))


# ---------------------------------------------------------------------------
# CRUD de reservas (solo admin)
# ---------------------------------------------------------------------------

@app.route('/reservas/nueva', methods=['GET', 'POST'])
@admin_required
def nueva_reserva():
    conn = get_db()
    salas = conn.execute("SELECT * FROM salas").fetchall()
    if request.method == 'POST':
        sala_id = request.form.get('sala_id')
        titulo = request.form.get('titulo', '').strip()
        fecha = request.form.get('fecha')
        hora_inicio = request.form.get('hora_inicio')
        hora_fin = request.form.get('hora_fin')
        solicitante_cargo = request.form.get('solicitante_cargo', '').strip()
        solicitante_nombre = request.form.get('solicitante_nombre', '').strip()

        # Validar solapamiento
        conflicto = conn.execute(
            '''SELECT id FROM reservas
               WHERE sala_id=? AND fecha=? AND estado='activa'
               AND NOT (hora_fin <= ? OR hora_inicio >= ?)''',
            (sala_id, fecha, hora_inicio, hora_fin)
        ).fetchone()
        if conflicto:
            flash('Conflicto de horario: la sala ya tiene una reserva en ese rango.', 'danger')
        elif hora_inicio >= hora_fin:
            flash('La hora de inicio debe ser anterior a la hora de fin.', 'danger')
        else:
            conn.execute(
                '''INSERT INTO reservas
                   (sala_id, usuario_id, titulo, fecha, hora_inicio, hora_fin, solicitante_cargo, solicitante_nombre)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (sala_id, session['user_id'], titulo, fecha, hora_inicio, hora_fin,
                 solicitante_cargo, solicitante_nombre)
            )
            conn.commit()
            flash('Reserva creada exitosamente.', 'success')
            conn.close()
            return redirect(url_for('historial'))
    conn.close()
    return render_template('nueva_reserva.html', salas=salas)


@app.route('/reservas/<int:reserva_id>/editar', methods=['GET', 'POST'])
@admin_required
def editar_reserva(reserva_id):
    conn = get_db()
    reserva = conn.execute("SELECT * FROM reservas WHERE id=?", (reserva_id,)).fetchone()
    salas = conn.execute("SELECT * FROM salas").fetchall()
    if not reserva:
        conn.close()
        flash('Reserva no encontrada.', 'danger')
        return redirect(url_for('historial'))

    if request.method == 'POST':
        sala_id = request.form.get('sala_id')
        titulo = request.form.get('titulo', '').strip()
        fecha = request.form.get('fecha')
        hora_inicio = request.form.get('hora_inicio')
        hora_fin = request.form.get('hora_fin')
        solicitante_cargo = request.form.get('solicitante_cargo', '').strip()
        solicitante_nombre = request.form.get('solicitante_nombre', '').strip()

        conflicto = conn.execute(
            '''SELECT id FROM reservas
               WHERE sala_id=? AND fecha=? AND estado='activa' AND id!=?
               AND NOT (hora_fin <= ? OR hora_inicio >= ?)''',
            (sala_id, fecha, reserva_id, hora_inicio, hora_fin)
        ).fetchone()
        if conflicto:
            flash('Conflicto de horario: la sala ya tiene una reserva en ese rango.', 'danger')
        elif hora_inicio >= hora_fin:
            flash('La hora de inicio debe ser anterior a la hora de fin.', 'danger')
        else:
            conn.execute(
                '''UPDATE reservas SET sala_id=?, titulo=?, fecha=?, hora_inicio=?, hora_fin=?,
                   solicitante_cargo=?, solicitante_nombre=?
                   WHERE id=?''',
                (sala_id, titulo, fecha, hora_inicio, hora_fin,
                 solicitante_cargo, solicitante_nombre, reserva_id)
            )
            conn.commit()
            flash('Reserva actualizada.', 'success')
            conn.close()
            return redirect(url_for('historial'))
    conn.close()
    return render_template('editar_reserva.html', reserva=reserva, salas=salas)


@app.route('/reservas/<int:reserva_id>/anular', methods=['POST'])
@admin_required
def anular_reserva(reserva_id):
    conn = get_db()
    conn.execute("UPDATE reservas SET estado='anulada' WHERE id=?", (reserva_id,))
    conn.commit()
    conn.close()
    flash('Reserva anulada.', 'warning')
    return redirect(url_for('historial'))


# ---------------------------------------------------------------------------
# Historial
# ---------------------------------------------------------------------------

@app.route('/historial')
@login_required
def historial():
    conn = get_db()
    salas = conn.execute("SELECT * FROM salas").fetchall()
    sala_id = request.args.get('sala_id', '')
    estado = request.args.get('estado', '')
    query = '''
        SELECT r.*, s.nombre AS sala_nombre, u.nombre AS usuario_nombre
        FROM reservas r
        JOIN salas s ON r.sala_id = s.id
        JOIN usuarios u ON r.usuario_id = u.id
        WHERE 1=1
    '''
    params = []
    if sala_id:
        query += " AND r.sala_id=?"
        params.append(sala_id)
    if estado:
        query += " AND r.estado=?"
        params.append(estado)
    query += " ORDER BY r.fecha DESC, r.hora_inicio DESC"
    reservas = conn.execute(query, params).fetchall()
    conn.close()
    return render_template('historial.html', reservas=reservas, salas=salas,
                           filtro_sala=sala_id, filtro_estado=estado)


# ---------------------------------------------------------------------------
# Gestión de usuarios (solo admin)
# ---------------------------------------------------------------------------

@app.route('/admin/usuarios')
@admin_required
def admin_usuarios():
    conn = get_db()
    usuarios = conn.execute("SELECT id, nombre, email, rol FROM usuarios ORDER BY nombre").fetchall()
    conn.close()
    return render_template('admin_usuarios.html', usuarios=usuarios)


@app.route('/admin/usuarios/nuevo', methods=['GET', 'POST'])
@admin_required
def admin_nuevo_usuario():
    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        rol = request.form.get('rol', 'usuario')
        conn = get_db()
        existente = conn.execute("SELECT id FROM usuarios WHERE email=?", (email,)).fetchone()
        if existente:
            flash('El correo ya está registrado.', 'danger')
        else:
            conn.execute(
                "INSERT INTO usuarios (nombre, email, password, rol) VALUES (?,?,?,?)",
                (nombre, email, generate_password_hash(password), rol)
            )
            conn.commit()
            flash('Usuario creado exitosamente.', 'success')
            conn.close()
            return redirect(url_for('admin_usuarios'))
        conn.close()
    return render_template('admin_nuevo_usuario.html')


@app.route('/admin/usuarios/<int:uid>/eliminar', methods=['POST'])
@admin_required
def admin_eliminar_usuario(uid):
    if uid == session['user_id']:
        flash('No puedes eliminar tu propia cuenta.', 'danger')
        return redirect(url_for('admin_usuarios'))
    conn = get_db()
    conn.execute("DELETE FROM usuarios WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    flash('Usuario eliminado.', 'warning')
    return redirect(url_for('admin_usuarios'))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000, use_reloader=False)
