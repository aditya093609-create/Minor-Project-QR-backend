import sqlite3
import uuid
import time
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime

app = Flask(__name__)
# Enable CORS for all routes, allowing frontend to connect from different domain/port
CORS(app)

DATABASE = 'attendance.db'

# --- Database Setup and Initialization ---

def get_db_connection():
    """Connects to the SQLite database."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row  # Access columns by name
    return conn

def initialize_db():
    """Creates tables if they do not exist, including the new 'rollno' column."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Updated users table to include rollno
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL, -- 'admin' or 'student'
            rollno TEXT UNIQUE
        )
    """)

    # Session table remains the same
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            qr_token TEXT PRIMARY KEY,
            class_name TEXT NOT NULL,
            class_code TEXT NOT NULL,
            timestamp REAL NOT NULL
        )
    """)

    # Attendance table remains the same
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            qr_token TEXT NOT NULL,
            status TEXT NOT NULL, -- 'Present' or 'Absent'
            timestamp REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()

# Initialize the database on startup
initialize_db()


# --- Authentication and Registration Routes ---

@app.route('/register', methods=['POST'])
def register():
    """Registers a new user (admin or student)."""
    data = request.json
    name = data.get('name')
    username = data.get('username')
    password = data.get('password')
    role = data.get('role')
    rollno = data.get('rollno') # New field

    if not all([name, username, password, role]):
        return jsonify({"error": "Missing required fields: name, username, password, role"}), 400
    
    if role not in ['admin', 'student']:
        return jsonify({"error": "Invalid role specified"}), 400
    
    # Roll number is required only for students
    if role == 'student' and not rollno:
         return jsonify({"error": "Students must provide a Roll Number"}), 400

    conn = get_db_connection()
    try:
        user_id = str(uuid.uuid4())
        # Insert user data, including rollno
        conn.execute("INSERT INTO users (id, name, username, password, role, rollno) VALUES (?, ?, ?, ?, ?, ?)",
                     (user_id, name, username, password, role, rollno))
        conn.commit()
        return jsonify({"message": f"{role.capitalize()} registration successful. You can now login.", "user_id": user_id}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username or Roll Number already exists."}), 409
    finally:
        conn.close()

@app.route('/login', methods=['POST'])
def login():
    """Logs in a user and returns user details and role."""
    data = request.json
    username = data.get('username')
    password = data.get('password')

    conn = get_db_connection()
    user_row = conn.execute("SELECT id, name, username, role, rollno FROM users WHERE username = ? AND password = ?", 
                            (username, password)).fetchone()
    conn.close()

    if user_row:
        user = dict(user_row)
        user['message'] = "Login successful."
        user['user_id'] = user.pop('id')
        
        # Ensure rollno is included in the response for the frontend state
        user['rollno'] = user.get('rollno') 
        
        return jsonify(user), 200
    else:
        return jsonify({"error": "Invalid username or password"}), 401


# --- Admin Routes ---

@app.route('/admin/create_session', methods=['POST'])
def create_session():
    """Creates a new attendance session and generates a QR token."""
    data = request.json
    class_name = data.get('class_name')
    class_code = data.get('class_code')

    if not all([class_name, class_code]):
        return jsonify({"error": "Missing class name or code"}), 400
    
    qr_token = str(uuid.uuid4())[:8].upper()
    timestamp = time.time()

    conn = get_db_connection()
    try:
        # Clear any existing active session (for simplicity in this single-QR app)
        conn.execute("DELETE FROM sessions")
        
        conn.execute("INSERT INTO sessions (qr_token, class_name, class_code, timestamp) VALUES (?, ?, ?, ?)",
                     (qr_token, class_name, class_code, timestamp))
        conn.commit()
        return jsonify({
            "message": "Session created successfully.",
            "qr_token": qr_token,
            "class_name": class_name,
            "class_code": class_code
        }), 201
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500
    finally:
        conn.close()

@app.route('/admin/attendance', methods=['GET'])
def admin_attendance_data():
    """Fetches all attendance records and student attendance statistics."""
    conn = get_db_connection()
    
    # 1. Fetch All Attendance Records (joining to get student name and rollno)
    records_cursor = conn.execute("""
        SELECT 
            a.id, a.student_id, a.qr_token, a.status, a.timestamp, 
            u.name as student_name, u.rollno as roll_no, 
            s.class_name, s.class_code 
        FROM attendance a
        JOIN users u ON a.student_id = u.id
        JOIN sessions s ON a.qr_token = s.qr_token
        ORDER BY a.timestamp DESC
    """)
    records = [dict(row) for row in records_cursor.fetchall()]

    # 2. Fetch Attendance Statistics
    # Get all unique students
    students_cursor = conn.execute("SELECT id, name, rollno FROM users WHERE role = 'student'")
    students = students_cursor.fetchall()

    # Get all unique classes (sessions)
    total_classes_cursor = conn.execute("SELECT COUNT(DISTINCT qr_token) as count FROM sessions")
    total_classes = total_classes_cursor.fetchone()['count']
    
    stats = []
    for student in students:
        attended_cursor = conn.execute("SELECT COUNT(*) as count FROM attendance WHERE student_id = ? AND status = 'Present'", (student['id'],))
        attended = attended_cursor.fetchone()['count']
        
        percentage = (attended / total_classes * 100) if total_classes > 0 else 0
        
        stats.append({
            "id": student['id'],
            "name": student['name'],
            "rollno": student['rollno'],
            "attended": attended,
            "total": total_classes,
            "percentage": round(percentage, 1)
        })

    # 3. Get Current Active QR Token
    current_session = conn.execute("SELECT qr_token FROM sessions ORDER BY timestamp DESC LIMIT 1").fetchone()
    current_qr_token = current_session['qr_token'] if current_session else None
    
    conn.close()

    return jsonify({
        "records": records,
        "stats": stats,
        "current_qr_token": current_qr_token
    }), 200

@app.route('/admin/update_attendance', methods=['POST'])
def update_attendance():
    """Allows admin to manually change an attendance record status."""
    data = request.json
    record_id = data.get('record_id')
    status = data.get('status')

    if not all([record_id, status]) or status not in ['Present', 'Absent']:
        return jsonify({"error": "Invalid record ID or status"}), 400

    conn = get_db_connection()
    conn.execute("UPDATE attendance SET status = ? WHERE id = ?", (status, record_id))
    conn.commit()
    conn.close()
    
    return jsonify({"message": f"Record {record_id} updated to {status}."}), 200


# --- Student Routes ---

@app.route('/student/mark_attendance', methods=['POST'])
def mark_attendance():
    """Allows a student to mark attendance using a QR token."""
    data = request.json
    student_id = data.get('student_id')
    qr_token_raw = data.get('qr_token')

    # 🌟 CRITICAL FIX: Ensure the token is always cleaned (trimmed) and converted to a consistent case (e.g., UPPER).
    qr_token = qr_token_raw.strip().upper() 

    conn = get_db_connection()
    
    # 1. Check if the session is active (Lookup uses the UPPERCASE token)
    session_row = conn.execute("SELECT class_code, class_name FROM sessions WHERE qr_token = ?", (qr_token,)).fetchone()
    if not session_row:
        conn.close()
        # This is where the error is coming from.
        return jsonify({"error": "Invalid or expired QR code/session."}), 400
    
    # 2. Check if student has already marked attendance for this session
    # Note: Use the cleaned, uppercase token here too.
    already_marked = conn.execute("SELECT id FROM attendance WHERE student_id = ? AND qr_token = ?", 
                                  (student_id, qr_token)).fetchone()
    if already_marked:
        conn.close()
        return jsonify({"error": "Attendance already marked for this session."}), 400

    # 3. Mark attendance
    timestamp = time.time()
    conn.execute("INSERT INTO attendance (student_id, qr_token, status, timestamp) VALUES (?, ?, ?, ?)",
                 (student_id, qr_token, 'Present', timestamp))
    conn.commit()
    conn.close()

    return jsonify({
        "message": f"Attendance marked for {session_row['class_code']}: {session_row['class_name']}.",
        "status": "Present"
    }), 200

@app.route('/student/stats/<student_id>', methods=['GET'])
def student_stats(student_id):
    """Fetches attendance statistics for a specific student."""
    conn = get_db_connection()

    # Get total unique sessions
    total_classes_cursor = conn.execute("SELECT COUNT(DISTINCT qr_token) as count FROM sessions").fetchone()
    total_classes = total_classes_cursor['count']
    
    # 🌟 CRITICAL FIX: Use COUNT(DISTINCT qr_token) to count unique sessions attended
    attended_cursor = conn.execute("SELECT COUNT(DISTINCT qr_token) as count FROM attendance WHERE student_id = ? AND status = 'Present'", (student_id,)).fetchone()
    attended = attended_cursor['count']
    
    conn.close()

    percentage = (attended / total_classes * 100) if total_classes > 0 else 0
    
    # Missed calculation will now be correct as 'attended' <= 'total_classes'
    missed = total_classes - attended

    return jsonify({
        "total_classes": total_classes,
        "attended": attended,
        "missed": missed,
        "percentage": round(percentage, 1)
    }), 200


if __name__ == '__main__':
    # Use 0.0.0.0 for development if running in a container/VM, 
    # but 127.0.0.1 (localhost) is fine for local testing.
    app.run(debug=True, host='127.0.0.1', port=5000)
