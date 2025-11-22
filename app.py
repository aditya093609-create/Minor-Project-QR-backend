import os
import uuid
import time
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
# Note: We removed 'from sqlalchemy.engine import URL'
from sqlalchemy import create_engine, text, Column, String, Float, TIMESTAMP
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv

# Load environment variables from a .env file locally (ignored in production on Render)
load_dotenv()

app = Flask(__name__)
# Enable CORS for frontend communication
CORS(app) 

# --- Database Configuration ---

# The connection string is read from the Render environment variable
DATABASE_URL = os.environ.get('DATABASE_URL')

if not DATABASE_URL:
    print("FATAL: DATABASE_URL environment variable is not set. Cannot connect to Singlestore.")
    exit(1) 

try:
    # 1. Pass the DATABASE_URL string directly to create_engine.
    # This avoids the incompatible URL object methods that caused the crash.
    engine = create_engine(
        DATABASE_URL, 
        # 2. Add the mandatory SSL connection arguments for Singlestore/Cloud MySQL
        connect_args={
            "ssl": {
                "ssl_mode": "preferred"
            }
        },
        pool_timeout=15 
    )
    
    # Setup Session and Base objects
    Session = sessionmaker(bind=engine)
    Base = declarative_base()

except Exception as e:
    # This error message will now only show if the connection string itself is bad
    print(f"ERROR: Failed to create SQLAlchemy engine and check Singlestore connection: {e}")
    exit(1)

# --- Database Models (Define your tables) ---

class User(Base):
    __tablename__ = 'users'
    id = Column(String(36), primary_key=True)
    name = Column(String(100), nullable=False)
    username = Column(String(50), unique=True, nullable=False)
    password = Column(String(100), nullable=False)
    role = Column(String(10), nullable=False) # 'admin' or 'student'
    rollno = Column(String(20), unique=True, nullable=True) # Unique for students

class SessionRecord(Base):
    __tablename__ = 'sessions'
    qr_token = Column(String(36), primary_key=True)
    class_name = Column(String(100), nullable=False)
    class_code = Column(String(20), nullable=False)
    timestamp = Column(Float, nullable=False) # Unix timestamp

class Attendance(Base):
    __tablename__ = 'attendance'
    id = Column(String(36), primary_key=True)
    student_id = Column(String(36), nullable=False)
    qr_token = Column(String(36), nullable=False)
    status = Column(String(10), nullable=False) # 'Present' or 'Absent'
    # TIMESTAMP is the standard MySQL type
    timestamp = Column(TIMESTAMP, nullable=False)

# --- Initialization Function ---

def initialize_db():
    """
    Called when the application starts. 
    Creates all defined tables if they don't exist in the connected database.
    """
    try:
        print("Attempting to connect to Singlestore and create tables...")
        # This is the line that connects and creates the tables
        Base.metadata.create_all(engine)
        print("Database tables ensured to exist.")
    except Exception as e:
        print(f"FATAL: Database initialization error. Check DATABASE_URL and Singlestore service: {e}")
        raise e

# --- Utility Functions ---

def get_db_session():
    """Returns a new SQLAlchemy session."""
    return Session()

# --- API Routes ---

@app.route('/register', methods=['POST'])
def register():
    """Handles new user registration."""
    data = request.get_json()
    name = data.get('name')
    username = data.get('username')
    password = data.get('password') 
    role = data.get('role')
    rollno = data.get('rollno', None)

    if not all([username, password, role]):
        return jsonify({"error": "Missing required fields."}), 400

    db_session = get_db_session()
    try:
        # Check if username or (if student) rollno already exists
        if db_session.query(User).filter_by(username=username).first():
            return jsonify({"error": "Username already exists."}), 409
        
        if role == 'student' and rollno and db_session.query(User).filter_by(rollno=rollno).first():
            return jsonify({"error": "Roll Number already registered."}), 409
        
        new_user = User(
            id=str(uuid.uuid4()),
            name=name,
            username=username,
            password=password,
            role=role,
            rollno=rollno if role == 'student' else None
        )

        db_session.add(new_user)
        db_session.commit()

        return jsonify({
            "message": f"User {username} registered successfully.",
            "user_id": new_user.id,
            "role": new_user.role
        }), 201

    except IntegrityError:
        db_session.rollback()
        return jsonify({"error": "Username or Roll Number already exists."}), 409
    except Exception as e:
        db_session.rollback()
        print(f"Registration error: {e}")
        return jsonify({"error": "An internal error occurred during registration."}), 500
    finally:
        db_session.close()


@app.route('/login', methods=['POST'])
def login():
    """Handles user login."""
    data = request.get_json()
    username = data.get('username')
    password = data.get('password') 

    db_session = get_db_session()
    user = db_session.query(User).filter_by(username=username).first()
    db_session.close()

    if user and user.password == password: 
        return jsonify({
            "message": "Login successful!",
            "user_id": user.id,
            "name": user.name,
            "role": user.role,
            "rollno": user.rollno
        }), 200
    else:
        return jsonify({"error": "Invalid username or password."}), 401


# --- ADMIN Routes ---

@app.route('/admin/create_session', methods=['POST'])
def create_session():
    """Creates a new attendance session and QR token."""
    data = request.get_json()
    class_name = data.get('class_name')
    class_code = data.get('class_code')

    if not all([class_name, class_code]):
        return jsonify({"error": "Missing class name or code."}), 400

    # Generate a unique token
    qr_token = str(uuid.uuid4())
    current_time = time.time() # Unix timestamp

    db_session = get_db_session()
    try:
        new_session = SessionRecord(
            qr_token=qr_token,
            class_name=class_name,
            class_code=class_code,
            timestamp=current_time
        )
        db_session.add(new_session)
        db_session.commit()

        return jsonify({
            "message": f"Session for {class_code} created.",
            "qr_token": qr_token
        }), 201
    except Exception as e:
        db_session.rollback()
        print(f"Session creation error: {e}")
        return jsonify({"error": "Internal error creating session."}), 500
    finally:
        db_session.close()


@app.route('/admin/attendance', methods=['GET'])
def admin_attendance():
    """Fetches all attendance records and student statistics."""
    db_session = get_db_session()
    try:
        # 1. Fetch All Records (Detailed View)
        records_query = db_session.query(
            Attendance.id,
            User.name.label('student_name'),
            User.rollno.label('roll_no'),
            SessionRecord.class_name,
            SessionRecord.class_code,
            Attendance.status,
            Attendance.timestamp
        ).join(User, Attendance.student_id == User.id)\
         .join(SessionRecord, Attendance.qr_token == SessionRecord.qr_token)\
         .order_by(Attendance.timestamp.desc())

        records = [{
            "id": rec.id,
            "student_name": rec.student_name,
            "roll_no": rec.roll_no,
            "class_name": rec.class_name,
            "class_code": rec.class_code,
            "status": rec.status,
            "timestamp": rec.timestamp.isoformat() if rec.timestamp else None
        } for rec in records_query.all()]

        # 2. Calculate Student Stats (Summary View)
        total_classes = db_session.query(SessionRecord.qr_token).distinct().count()

        stats_query = db_session.query(
            User.name,
            User.rollno,
            Attendance.student_id,
            text("COUNT(CASE WHEN attendance.status = 'Present' THEN 1 END) as attended_count")
        ).join(Attendance, User.id == Attendance.student_id, isouter=True)\
         .filter(User.role == 'student')\
         .group_by(User.id, User.name, User.rollno) 

        stats = []
        for stat in stats_query.all():
            attended = stat.attended_count or 0
            percentage = (attended / total_classes * 100) if total_classes > 0 else 0
            
            stats.append({
                "name": stat.name,
                "rollno": stat.rollno,
                "attended": attended,
                "total": total_classes,
                "percentage": round(percentage, 1)
            })

        return jsonify({"records": records, "stats": stats, "current_qr_token": None}), 200

    except Exception as e:
        print(f"Admin data fetch error: {e}")
        return jsonify({"error": "Internal error fetching admin data."}), 500
    finally:
        db_session.close()


@app.route('/admin/update_attendance', methods=['POST'])
def update_attendance():
    """Manually updates an attendance record status."""
    data = request.get_json()
    record_id = data.get('record_id')
    new_status = data.get('status')

    if not all([record_id, new_status in ['Present', 'Absent']]):
        return jsonify({"error": "Invalid record ID or status."}), 400

    db_session = get_db_session()
    try:
        attendance_record = db_session.query(Attendance).filter_by(id=record_id).first()
        
        if not attendance_record:
            return jsonify({"error": "Attendance record not found."}), 404

        attendance_record.status = new_status
        db_session.commit()
        
        return jsonify({"message": f"Record {record_id} updated to {new_status}."}), 200
    except Exception as e:
        db_session.rollback()
        print(f"Attendance update error: {e}")
        return jsonify({"error": "Internal error updating attendance."}), 500
    finally:
        db_session.close()


# --- STUDENT Routes ---

@app.route('/student/mark_attendance', methods=['POST'])
def mark_attendance():
    """Marks attendance for a student using a QR token."""
    data = request.get_json()
    student_id = data.get('student_id')
    qr_token = data.get('qr_token')

    db_session = get_db_session()
    try:
        # 1. Check if the session is valid/exists
        session_record = db_session.query(SessionRecord).filter_by(qr_token=qr_token).first()
        if not session_record:
            return jsonify({"error": "Invalid or expired QR code/session."}), 404
        
        # 2. Check if student is already marked present for this session
        existing_record = db_session.query(Attendance).filter_by(
            student_id=student_id, 
            qr_token=qr_token
        ).first()

        if existing_record and existing_record.status == 'Present':
            return jsonify({
                "message": f"You are already marked Present for {session_record.class_code}."
            }), 200
        
        timestamp = datetime.now()

        if existing_record:
            # Update existing 'Absent' record to 'Present'
            existing_record.status = 'Present'
            existing_record.timestamp = timestamp
        else:
            # Create a new attendance record
            new_record = Attendance(
                id=str(uuid.uuid4()),
                student_id=student_id,
                qr_token=qr_token,
                status='Present',
                timestamp=timestamp
            )
            db_session.add(new_record)

        db_session.commit()

        return jsonify({
            "message": f"Attendance marked for {session_record.class_code}: {session_record.class_name}.",
            "status": "Present"
        }), 200

    except Exception as e:
        db_session.rollback()
        print(f"Attendance marking error: {e}")
        return jsonify({"error": "An internal error occurred while marking attendance."}), 500
    finally:
        db_session.close()


@app.route('/student/stats/<student_id>', methods=['GET'])
def student_stats(student_id):
    """Fetches attendance statistics for a specific student."""
    db_session = get_db_session()
    try:
        # Get total unique sessions
        total_classes = db_session.query(SessionRecord.qr_token).distinct().count()
        
        # Get classes attended (marked as 'Present')
        attended = db_session.query(Attendance).filter(
            Attendance.student_id == student_id,
            Attendance.status == 'Present'
        ).count()
        
        db_session.close()

        percentage = (attended / total_classes * 100) if total_classes > 0 else 0
        missed = total_classes - attended

        return jsonify({
            "total_classes": total_classes,
            "attended": attended,
            "missed": missed,
            "percentage": round(percentage, 1)
        }), 200
    except Exception as e:
        print(f"Student stats error: {e}")
        return jsonify({"error": "Internal error fetching stats."}), 500
    finally:
        db_session.close()


if __name__ == '__main__':
    # This block is for local development only and is ignored by Gunicorn on Render.
    try:
        with app.app_context():
            initialize_db() 
        app.run(debug=True, port=os.environ.get('PORT', 5000))
    except Exception as e:
        print(f"Application failed to start locally: {e}")
