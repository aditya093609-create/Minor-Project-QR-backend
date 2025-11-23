import sqlite3
import uuid
import time
from flask import Flask, request, jsonify
from flask_cors import CORS
# 🔑 NEW: Import timedelta to handle date calculations
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
CORS(app)

DATABASE = 'attendance.db'

# --- Database Setup and Initialization ---

def get_db_connection():
    """Connects to the SQLite database."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row  # Access columns by name
    return conn

def initialize_db():
    """Creates tables with all necessary fields (class_id, semester) and hashed passwords."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 🌟 USERS TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL, 
            role TEXT NOT NULL,
            rollno TEXT UNIQUE,
            class_id TEXT,       
            semester TEXT        
        )
    """)

    # 🌟 SESSIONS TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            qr_token TEXT PRIMARY KEY,
            class_name TEXT NOT NULL,
            class_code TEXT NOT NULL,
            timestamp REAL NOT NULL,
            class_id TEXT NOT NULL    -- Class identifier for filtering
        )
    """)

    # 🌟 ATTENDANCE TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            qr_token TEXT NOT NULL,
            status TEXT NOT NULL, 
            timestamp REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()

initialize_db()

# --- Authentication and Registration Routes ---

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    name = data.get('name')
    username = data.get('username')
    password = data.get('password')
    role = data.get('role')
    rollno = data.get('rollno')
    class_id = data.get('class_id') 
    semester = data.get('semester') 

    if not all([name, username, password, role, class_id]):
        return jsonify({"error": "Missing required fields: name, username, password, role, class_id"}), 400
    
    if role not in ['admin', 'student']:
        return jsonify({"error": "Invalid role specified"}), 400
    
    if role == 'student' and not all([rollno, semester]):
         return jsonify({"error": "Students must provide a Roll Number and Semester"}), 400

    conn = get_db_connection()
    try:
        user_id = str(uuid.uuid4())
        hashed_password = generate_password_hash(password) # 🔑 Secure Hashing

        if role == 'admin':
            sql = "INSERT INTO users (id, name, username, password, role, class_id) VALUES (?, ?, ?, ?, ?, ?)"
            params = (user_id, name, username, hashed_password, role, class_id)
        else: # student
            sql = "INSERT INTO users (id, name, username, password, role, rollno, class_id, semester) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            params = (user_id, name, username, hashed_password, role, rollno, class_id, semester)

        conn.execute(sql, params)
        conn.commit()
        return jsonify({"message": f"{role.capitalize()} registration successful. You can now login.", "user_id": user_id}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username or Roll Number already exists."}), 409
    finally:
        conn.close()

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    conn = get_db_connection()
    user_row = conn.execute("SELECT id, name, username, role, rollno, password, class_id, semester FROM users WHERE username = ?", 
                            (username,)).fetchone()
    conn.close()

    if user_row:
        user = dict(user_row)
        # 🔑 Check password hash
        if check_password_hash(user['password'], password):
            return jsonify({
                "message": "Login successful.",
                "user": { 
                    "user_id": user['id'],
                    "name": user['name'],
                    "username": user['username'],
                    "role": user['role'],
                    "rollno": user.get('rollno'),
                    "class_id": user.get('class_id'),   
                    "semester": user.get('semester')  
                }
            }), 200
        else:
            return jsonify({"error": "Invalid username or password"}), 401
    else:
        return jsonify({"error": "Invalid username or password"}), 401

# --- Admin Routes (Filtered by class_id & Date) ---

@app.route('/admin/create_session', methods=['POST'])
def create_session():
    data = request.json
    class_name = data.get('class_name')
    class_code = data.get('class_code')
    admin_class_id = data.get('class_id') 
    session_date_str = data.get('date') # Gets date from frontend

    if not all([class_name, class_code, admin_class_id, session_date_str]):
        return jsonify({"error": "Missing required fields"}), 400
    
    qr_token = str(uuid.uuid4())[:8].upper()
    
    try:
        session_date = datetime.strptime(session_date_str, '%Y-%m-%d')
        current_time = datetime.now().time()
        session_datetime = datetime.combine(session_date, current_time)
        timestamp = session_datetime.timestamp()
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    conn = get_db_connection()
    try:
        # 🚨 CRITICAL: I have REMOVED the 'DELETE FROM sessions' line.
        # This ensures old attendance data stays in the database.
        
        conn.execute("INSERT INTO sessions (qr_token, class_name, class_code, timestamp, class_id) VALUES (?, ?, ?, ?, ?)",
                     (qr_token, class_name, class_code, timestamp, admin_class_id))
        conn.commit()
        
        return jsonify({
            "qr_token": qr_token,
            "class_name": class_name,
            "class_code": class_code
        }), 201
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500
    finally:
        conn.close()

@app.route('/admin/attendance', methods=['POST'])
def admin_attendance_data():
    """
    Fetches attendance records and statistics filtered by class_id.
    🌟 NEW: Accepts optional 'date' parameter to filter by specific day.
    """
    data = request.json
    class_id = data.get('class_id') 
    target_date_str = data.get('date') # Format: YYYY-MM-DD

    if not class_id:
        return jsonify({"error": "Class ID is required."}), 400
        
    conn = get_db_connection()

    # --- Date Filtering Logic ---
    date_filter_sql = ""
    date_params = []
    
    if target_date_str:
        try:
            target_date = datetime.strptime(target_date_str, '%Y-%m-%d')
            start_of_day = target_date.timestamp()
            end_of_day = (target_date + timedelta(days=1)).timestamp()
            
            # This SQL fragment will be added to queries to filter sessions by time
            date_filter_sql = " AND s.timestamp >= ? AND s.timestamp < ?"
            date_params = [start_of_day, end_of_day]
        except ValueError:
            pass # Ignore invalid dates

    # 1. Fetch Students (Always all students in the class)
    students_cursor = conn.execute("SELECT id, name, rollno FROM users WHERE role = 'student' AND class_id = ?", (class_id,)).fetchall()
    
    # 2. Get Total Sessions (Filtered by class AND date)
    sessions_query = "SELECT COUNT(qr_token) as count FROM sessions s WHERE class_id = ?" + date_filter_sql
    total_classes_cursor = conn.execute(sessions_query, (class_id, *date_params)).fetchone()
    total_sessions_count = total_classes_cursor['count']
    
    stats = []
    for student in students_cursor:
        # 3. Get Student Attendance (Filtered by class AND date)
        attended_query = f"""
            SELECT COUNT(T1.id) AS count FROM attendance T1
            JOIN sessions s ON T1.qr_token = s.qr_token
            WHERE T1.student_id = ? AND T1.status = 'Present' AND s.class_id = ? {date_filter_sql}
        """
        attended_params = (student['id'], class_id, *date_params)
        
        attended_cursor = conn.execute(attended_query, attended_params).fetchone()
        attended = attended_cursor['count']
        
        # Calculate percentage
        percentage = (attended / total_sessions_count * 100) if total_sessions_count > 0 else 0
        
        stats.append({
            "id": student['id'],
            "name": student['name'],
            "rollno": student['rollno'],
            "attended": attended,
            "total": total_sessions_count,
            "percentage": round(percentage, 1)
        })

    # 4. Fetch Detailed Records (Filtered by class AND date)
    records_query = f"""
        SELECT 
            a.id, a.student_id, a.qr_token, a.status, a.timestamp, 
            u.name as student_name, u.rollno as roll_no, 
            s.class_name, s.class_code 
        FROM attendance a
        JOIN users u ON a.student_id = u.id
        JOIN sessions s ON a.qr_token = s.qr_token
        WHERE s.class_id = ? AND u.class_id = ? {date_filter_sql}
        ORDER BY a.timestamp DESC
    """
    records_params = (class_id, class_id, *date_params)
    records_cursor = conn.execute(records_query, records_params)
    records = [dict(row) for row in records_cursor.fetchall()]

    # 5. Get Current Active QR Token (Always latest, not filtered by date)
    current_session = conn.execute("SELECT qr_token FROM sessions WHERE class_id = ? ORDER BY timestamp DESC LIMIT 1", (class_id,)).fetchone()
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

@app.route('/admin/delete_student', methods=['POST'])
def delete_student():
    """Deletes a student and all their records. Simplified to work reliably."""
    data = request.json
    student_id = data.get('student_id')

    if not student_id:
        return jsonify({"error": "Missing student ID"}), 400

    conn = get_db_connection()
    try:
        # CRITICAL 1: Delete attendance records first (Foreign Key protection)
        conn.execute("DELETE FROM attendance WHERE student_id = ?", (student_id,))
        
        # CRITICAL 2: Delete user. 
        # Removing class_id check here to ensure deletion works if ID is correct.
        result = conn.execute("DELETE FROM users WHERE id = ? AND role = 'student'", (student_id,))
        
        conn.commit()
        
        if result.rowcount == 0:
            return jsonify({"error": "Student not found."}), 404

        return jsonify({"message": "Student deleted successfully"}), 200
    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"Database error during deletion: {str(e)}"}), 500
    finally:
        conn.close()

# --- Student Routes ---

@app.route('/student/mark_attendance', methods=['POST'])
def mark_attendance():
    """Allows a student to mark attendance using a QR token."""
    data = request.json
    student_id = data.get('student_id')
    qr_token = data.get('qr_token')

    conn = get_db_connection()
    
    # 1. Check if the session is active
    session_row = conn.execute("SELECT class_code, class_name FROM sessions WHERE qr_token = ?", (qr_token,)).fetchone()
    if not session_row:
        conn.close()
        return jsonify({"error": "Invalid or expired QR code/session."}), 400
    
    # 2. Check if student has already marked attendance for this session
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

    # Get student's class_id
    student_user_data = conn.execute("SELECT class_id FROM users WHERE id = ?", (student_id,)).fetchone()
    if not student_user_data:
        conn.close()
        return jsonify({"error": "Student not found."}), 404

    class_id = student_user_data['class_id']
    
    # Get total unique sessions for the student's class
    total_classes_cursor = conn.execute("SELECT COUNT(qr_token) as count FROM sessions WHERE class_id = ?", (class_id,)).fetchone()
    total_classes = total_classes_cursor['count']
    
    # Get classes attended
    attended_cursor = conn.execute("""
        SELECT COUNT(T1.id) AS count FROM attendance T1
        JOIN sessions T2 ON T1.qr_token = T2.qr_token
        WHERE T1.student_id = ? AND T1.status = 'Present' AND T2.class_id = ?
    """, (student_id, class_id)).fetchone()
    attended = attended_cursor['count']
    
    conn.close()

    percentage = (attended / total_classes * 100) if total_classes > 0 else 0
    missed = total_classes - attended

    return jsonify({
        "total_classes": total_classes,
        "attended": attended,
        "missed": missed,
        "percentage": round(percentage, 1)
    }), 200

if __name__ == '__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)
