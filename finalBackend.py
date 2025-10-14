from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import logging
import psycopg2 
from datetime import datetime

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
CORS(app) 

# --- 1. DATABASE CONFIGURATION (Placeholders) ---
# IMPORTANT: Replace these placeholders with your actual credentials.
DB_CONFIG = {
    "host": "rerepprddb-as-a1p.dbaas.comcast.net",       # e.g., "localhost" or an IP address
    "database": "RE_Reporting_DB",
    "user": "re_rep_rw",
    "password": "rerepro!2025@",
    "port": "5432" # Default PostgreSQL port
}

def get_db_connection():
    """Establishes a connection to the PostgreSQL database."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        conn.autocommit = False # Use transactions for submission, though not strictly needed for COUNT
        return conn
    except psycopg2.Error as e:
        logging.error(f"Database connection failed: {e}")
        raise ConnectionError("Could not connect to the database.")

# --- 2. ENDPOINTS ---

@app.route('/lookup_employee', methods=['POST'])
def lookup_employee():
    """
    Performs a SELECT query against the 'trace_employees' table to fetch suggestions.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        data = request.get_json()
        email_prefix = data.get('email_prefix', '').strip().lower()

        if not email_prefix or len(email_prefix) < 2:
            return jsonify([]), 200

        query = (
            "SELECT u_email, name, dv_manager FROM public.trace_employees "
            "WHERE u_email ILIKE %s LIMIT 10"
        )
        
        cursor.execute(query, (email_prefix + '%',)) 
        
        results = cursor.fetchall()
        suggestions = [
            {"email": row[0], "name": row[1], "manager": row[2]}
            for row in results
        ]
        
        logging.info(f"DB Lookup for '{email_prefix}' found {len(suggestions)} suggestions.")
        
        cursor.close()
        return jsonify(suggestions), 200

    except ConnectionError:
        return jsonify({"error": "Database connection error."}), 503
    except Exception as e:
        logging.error(f"Error during employee lookup: {e}")
        return jsonify({"error": "Internal server error during DB query."}), 500
    finally:
        if conn:
            conn.close()


@app.route('/get_sop_count', methods=['GET'])
def get_sop_count():
    """
    Retrieves the total number of entries in the sop_requests table.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        query = "SELECT COUNT(id) FROM sop_requests"
        cursor.execute(query) 
        
        count = cursor.fetchone()[0]
        
        logging.info(f"Fetched total SOP request count: {count}")
        
        cursor.close()
        return jsonify({"count": count}), 200

    except ConnectionError:
        return jsonify({"error": "Database connection error."}), 503
    except Exception as e:
        logging.error(f"Error retrieving SOP count: {e}")
        return jsonify({"error": "Internal server error during count query."}), 500
    finally:
        if conn:
            conn.close()

@app.route('/get_all_submissions', methods=['GET'])
def get_all_submissions():
    """
    Retrieves a list of recent SOP request submissions for history view, including 'reason' and 'assigned_to'.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # MODIFIED: ADDED 'assigned_to' to the SELECT statement
        query = (
            "SELECT id, email, name, manager, short_description, auth_type, db_access, "
            "created_at, last_updated, completed_on, status, reason, assigned_to "
            "FROM sop_requests "
            "ORDER BY created_at DESC "
            "LIMIT 50"
        )
        cursor.execute(query) 
        
        # Get column names for building the list of dictionaries
        column_names = [desc[0] for desc in cursor.description]
        
        submissions = []
        for row in cursor.fetchall():
            submission = dict(zip(column_names, row))
            # Format the timestamp for better readability on the frontend
            if 'created_at' in submission and submission['created_at']:
                submission['created_at'] = submission['created_at'].strftime("%Y-%m-%d %H:%M:%S")

            if 'last_updated' in submission and submission['last_updated']:
                submission['last_updated'] = submission['last_updated'].strftime("%Y-%m-%d %H:%M:%S")
            if 'completed_on' in submission and submission['completed_on']:
                submission['completed_on'] = submission['completed_on'].strftime("%Y-%m-%d %H:%M:%S")
            submissions.append(submission)
        
        logging.info(f"Fetched {len(submissions)} recent SOP submissions.")
        
        cursor.close()
        
        # Return the list of submissions
        return jsonify({"submissions": submissions}), 200

    except ConnectionError:
        return jsonify({"error": "Database connection error."}), 503
    except Exception as e:
        logging.error(f"Error retrieving all submissions: {e}")
        return jsonify({"error": "Internal server error during history query."}), 500
    finally:
        if conn:
            conn.close()

@app.route('/submit_sop', methods=['POST'])
def submit_sop():
    """
    Performs an INSERT query into the 'sop_requests' table to save form data, including 'reason' and 'assigned_to'.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        sop_data = request.get_json()

        # Extract all necessary fields for insertion
        email = sop_data.get('email')
        name = sop_data.get('name')
        manager = sop_data.get('manager')
        short_desc = sop_data.get('shortDescription')
        auth_type = sop_data.get('authType')
        further_auth_info = sop_data.get('furtherAuthInfo', '')
        detailed_desc = sop_data.get('detailedDescription')
        db_access = sop_data.get('dbAccess')
        db_details = sop_data.get('dbDetails', '')
        status = sop_data.get('status', 'Submitted')
        reason = sop_data.get('reason', 'Initial submission.')
        # NEW: Extract the assigned_to field
        assigned_to = sop_data.get('assigned_to', 'Unassigned')
        
        # Serialize attachment info dictionary to a JSON string for database storage
        attachment_info_json = json.dumps(sop_data.get('attachment_info')) if sop_data.get('attachment_info') else None
        
        # MODIFIED: ADDED 'assigned_to' to the columns list and a corresponding '%s' placeholder
        insert_query = (
            "INSERT INTO sop_requests ("
            "email, name, manager, short_description, auth_type, further_auth_info, "
            "detailed_description, db_access, db_details, attachment_info, created_at, "
            "last_updated, status, reason, assigned_to"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, created_at"
        )

        # MODIFIED: ADDED 'assigned_to' to the values tuple
        now = datetime.now()
        cursor.execute(insert_query, (
            email, name, manager, short_desc, auth_type, further_auth_info,
            detailed_desc, db_access, db_details, attachment_info_json, now, now,
            status, reason, assigned_to
        ))
        
       
        result = cursor.fetchone()
        request_id = result[0]
        created_at = result[1].strftime("%Y-%m-%dT%H:%M:%SZ")
        
        conn.commit()
        
        logging.info(f"SOP submission successful for user: {email}. Request ID: {request_id}.")

        cursor.close()
        return jsonify({
            "success": True, 
            "message": "SOP Request submitted successfully and saved to the database.",
            "request_id": request_id,
            "created_at": created_at
        }), 200

    except ConnectionError:
        return jsonify({"success": False, "error": "Database connection error. Check server logs."}), 503
    except psycopg2.Error as e:
        if conn:
            conn.rollback()
        logging.error(f"Database INSERT failed: {e}")
        return jsonify({"success": False, "error": f"Database error during submission: {e}"}), 500
    except Exception as e:
        logging.error(f"Error processing SOP submission: {e}")
        return jsonify({"success": False, "error": "Failed to process submission on the server."}), 500
    finally:
        if conn:
            conn.close()

@app.route('/update_status', methods=['POST'])
def update_status():
    """
    Updates the status and reason of an existing SOP request.
    Also updates last_updated and completed_on timestamps.
    """
    conn = None
    try:
        data = request.get_json()
        request_id = data.get('request_id')
        new_status = data.get('new_status')
        new_reason = data.get('reason')

        if not request_id or not new_status or not new_reason:
            return jsonify({"error": "Missing 'request_id', 'new_status', or 'reason'."}), 400
        
        conn = get_db_connection()
        cursor = conn.cursor()

        now = datetime.now()
        
        # Update completed_on only if status is 'Completed'
        if new_status == 'Completed':
            update_query = """
                UPDATE sop_requests 
                SET status = %s, reason = %s, last_updated = %s, completed_on = %s 
                WHERE id = %s
                RETURNING last_updated, completed_on
            """
            cursor.execute(update_query, (new_status, new_reason, now, now, request_id))
        else:
            update_query = """
                UPDATE sop_requests 
                SET status = %s, reason = %s, last_updated = %s 
                WHERE id = %s
                RETURNING last_updated
            """
            cursor.execute(update_query, (new_status, new_reason, now, request_id))
        
        if cursor.rowcount == 0:
            conn.rollback()
            return jsonify({"error": "No record found with the provided request_id."}), 404

        result = cursor.fetchone()
        conn.commit()
        
        logging.info(f"Updated status for Request ID {request_id} to '{new_status}' with reason.")

        # Return the updated timestamps in ISO format
        response_data = {
            "success": True, 
            "message": "Status and reason updated successfully.",
            "last_updated": result[0].strftime("%Y-%m-%dT%H:%M:%SZ") if result[0] else None
        }
        
        if new_status == 'Completed' and len(result) > 1:
            response_data["completed_on"] = result[1].strftime("%Y-%m-%dT%H:%M:%SZ") if result[1] else None
        
        return jsonify(response_data), 200

    except ConnectionError:
        return jsonify({"success": False, "error": "Database connection error. Check server logs."}), 503
    except psycopg2.Error as e:
        if conn:
            conn.rollback()
        logging.error(f"Database UPDATE failed: {e}")
        return jsonify({"success": False, "error": f"Database error during status update: {e}"}), 500
    except Exception as e:
        logging.error(f"Error updating status: {e}")
        return jsonify({"success": False, "error": "Failed to update status on the server."}), 500
    finally:
        if conn:
            conn.close()


@app.route('/update_assignment', methods=['POST'])
def update_assignment():
    """
    Updates the 'assigned_to' field of an existing SOP request.
    Also updates the last_updated timestamp.
    """
    conn = None
    try:
        data = request.get_json()
        request_id = data.get('request_id')
        new_assignee = data.get('assigned_to')

        if not all([request_id, new_assignee]):
            return jsonify({"error": "Missing 'request_id' or 'assigned_to'."}), 400
        
        conn = get_db_connection()
        cursor = conn.cursor()

        now = datetime.now()
        update_query = """
            UPDATE sop_requests 
            SET assigned_to = %s, last_updated = %s 
            WHERE id = %s
            RETURNING last_updated
        """
        cursor.execute(update_query, (new_assignee, now, request_id))
        
        if cursor.rowcount == 0:
            conn.rollback()
            return jsonify({"error": "No record found with the provided request_id."}), 404

        result = cursor.fetchone()
        conn.commit()
        
        logging.info(f"Updated assignment for Request ID {request_id} to '{new_assignee}'.")
        
        return jsonify({
            "success": True, 
            "message": f"Assigned to {new_assignee} successfully.",
            "last_updated": result[0].strftime("%Y-%m-%dT%H:%M:%SZ") if result[0] else None
        }), 200

    except ConnectionError:
        return jsonify({"success": False, "error": "Database connection error. Check server logs."}), 503
    except psycopg2.Error as e:
        if conn:
            conn.rollback()
        logging.error(f"Database UPDATE failed for assignment: {e}")
        return jsonify({"success": False, "error": f"Database error during assignment update: {e}"}), 500
    except Exception as e:
        logging.error(f"Error updating assignment: {e}")
        return jsonify({"success": False, "error": "Failed to update assignment on the server."}), 500
    finally:
        if conn:
            conn.close()


if __name__ == '__main__':
    app.run(debug=True)