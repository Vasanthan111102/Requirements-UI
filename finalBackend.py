from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import logging
import psycopg2 
from datetime import datetime
import os
import configparser
import requests

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

def get_manager_email(manager_name):
    """Lookup manager's email from the trace_employees table."""
    if not manager_name:
        return None
        
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Search for manager's email by name
        query = """
            SELECT u_email 
            FROM public.trace_employees 
            WHERE name = %s 
            LIMIT 1
        """
        cursor.execute(query, (manager_name,))
        result = cursor.fetchone()
        
        return result[0] if result else None
        
    except Exception as e:
        logging.error(f"Error looking up manager email: {e}")
        return None
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def load_email_config():
    """Load MDP email API settings from config.ini [email] section."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini')
    parser = configparser.ConfigParser()
    if not os.path.exists(config_path):
        logging.error("config.ini not found next to app.py")
        return None
    try:
        parser.read(config_path)
        auth_user = parser.get('email', 'auth_user', fallback=None)
        auth_pass = parser.get('email', 'auth_pass', fallback=None)
        mdp_api = parser.get('email', 'mdp_api', fallback=None)
        if not auth_user or not auth_pass or not mdp_api:
            logging.error("Missing one or more required [email] settings: auth_user, auth_pass, mdp_api")
            return None
        return {"auth_user": auth_user, "auth_pass": auth_pass, "mdp_api": mdp_api}
    except Exception as e:
        logging.error(f"Failed to load email config: {e}")
        return None

def send_via_mdp_api_base(auth_user, auth_pass, mdp_endpoint, payload, headers=None):
    """Generic sender using an MDP-like REST API."""
    try:
        resp = requests.post(mdp_endpoint, json=payload, auth=(auth_user, auth_pass), headers=headers or {})
        resp.raise_for_status()
        return True, resp.json()
    except requests.RequestException as e:
        logging.error(f"MDP API request failed: {e}")
        try:
            detail = resp.text if 'resp' in locals() and hasattr(resp, 'text') else str(e)
            logging.error(f"Response body: {detail}")
        except Exception:
            pass
        return False, None

def send_email_notification(sop_data, request_id):
    """Send email via MDP API to requester and their manager (if found)."""
    logging.info("🔔 send_email_notification() function called!")
    logging.info(f"📧 Attempting to send email for Request ID: {request_id}")

    # Load MDP API credentials
    cfg = load_email_config()
    if not cfg:
        return False

    try:
        # Get recipient details
        recipient_email = sop_data.get('email')
        manager_name = sop_data.get('manager')
        
        if not recipient_email:
            logging.warning("⚠️ No recipient email provided in SOP data. Email not sent.")
            return False
        
        # Lookup manager's email
        manager_email = get_manager_email(manager_name)
        if manager_email:
            logging.info(f"👔 Found manager email: {manager_email}")
        else:
            logging.warning("⚠️ Could not find manager's email in the database")

        # Fetch canonical auth_type from DB for this request
        auth_type_value = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT auth_type FROM sop_requests WHERE id = %s", (request_id,))
            row = cur.fetchone()
            if row:
                auth_type_value = row[0]
        except Exception as e:
            logging.error(f"Failed to fetch auth_type for request {request_id}: {e}")
            # Fallback to payload values if present
            auth_type_value = sop_data.get('auth_type') or sop_data.get('authType')
        finally:
            try:
                if 'cur' in locals():
                    cur.close()
                if 'conn' in locals():
                    conn.close()
            except Exception:
                pass

        # Build common email content
        attachment_info = sop_data.get('attachment_info')
        if attachment_info and isinstance(attachment_info, str):
            try:
                attachment_info = json.loads(attachment_info)
            except Exception:
                attachment_info = None

        attachment_text = (
            f"{attachment_info['filename']} ({attachment_info['size']} bytes)"
            if attachment_info else "No attachment"
        )
        
        submission_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        def build_html(is_manager=False):
            recipient_type = "Manager" if is_manager else "User"
            action = "submitted for your approval" if is_manager else "submitted successfully"
            return f"""
            <html>
            <body style=\"font-family:Segoe UI,Arial,sans-serif;\">
                <h2>📋 SOP Request {action.title()}</h2>
                <p>Dear {sop_data.get('manager' if is_manager else 'name', recipient_type)},</p>
                <p>{'An SOP Request has been submitted by ' + sop_data.get('name', 'a user') + ' and requires your approval.' if is_manager else 'Your SOP Request'} (Request ID: <b>{request_id}</b>) has been {action} on {submission_time}.</p>
                <div style=\"background-color:#f8fafc; padding:12px; border-radius:6px; margin:12px 0;\">
                    <h3 style=\"margin-top:0; color:#1e40af;\">Request Details:</h3>
                    <table cellpadding=\"6\" cellspacing=\"0\" style=\"width:100%;\">
                        <tr><td style=\"width:40%; color:#4b5563;\"><b>Requested By:</b></td><td>{sop_data.get('name', 'N/A')} ({sop_data.get('email', 'N/A')})</td></tr>
                        <tr><td style=\"color:#4b5563;\"><b>Manager:</b></td><td>{sop_data.get('manager', 'N/A')}</td></tr>
                        <tr><td style=\"color:#4b5563;\"><b>Department:</b></td><td>{sop_data.get('department', 'N/A')}</td></tr>
                        <tr><td style=\"color:#4b5563;\"><b>Short Description:</b></td><td>{sop_data.get('shortDescription', 'N/A')}</td></tr>
                        <tr><td style=\"color:#4b5563;\"><b>Detailed Description:</b></td><td>{sop_data.get('detailedDescription', 'N/A')}</td></tr>
                        <tr><td style=\"color:#4b5563;\"><b>Authentication Information:</b></td><td>{auth_type_value or 'N/A'}</td></tr>
                        <tr><td style=\"color:#4b5563;\"><b>Attachment:</b></td><td>{attachment_text}</td></tr>
                        <tr><td style=\"color:#4b5563;\"><b>Status:</b></td><td>{sop_data.get('status', 'Submitted')}</td></tr>
                    </table>
                </div>
                <p style=\"color:#6b7280;font-size:12px;margin-top:24px;\">– SOP Request Management System</p>
            </body>
            </html>
            """

        def build_text(is_manager=False):
            action = "submitted for your approval" if is_manager else "submitted successfully"
            return (
                f"SOP Request {action}.\n"
                f"Request ID: {request_id}\n"
                f"Requested By: {sop_data.get('name', 'N/A')} ({sop_data.get('email', 'N/A')})\n"
                f"Manager: {sop_data.get('manager', 'N/A')}\n"
                f"Department: {sop_data.get('department', 'N/A')}\n"
                f"Short Description: {sop_data.get('shortDescription', 'N/A')}\n"
                f"Detailed Description: {sop_data.get('detailedDescription', 'N/A')}\n"
                f"Authentication Information: {auth_type_value or 'N/A'}\n"
                f"Attachment: {attachment_text}\n"
                f"Status: {sop_data.get('status', 'Submitted')}\n"
                f"Submitted at: {submission_time}\n"
            )

        # Prepare recipients list (send only to user for now)
        recipients = [recipient_email]
        # Disabled manager notifications for now:
        # if manager_email:
        #     recipients.append(manager_email)

        # MDP transmissions API expects 'recipients' and 'content'
        # NOTE: To CC the manager instead of adding as a recipient,
        # you can use the 'CC' header and also include the manager as a recipient
        # with 'header_to' set to the primary recipient. Example (COMMENTED OUT):
        #
        # primary_to = recipients[0]
        # cc_addresses = []
        # if manager_email:
        #     cc_addresses.append(manager_email)
        #
        # mdp_payload = {
        #     "recipients": (
        #         [{"address": {"email": primary_to}}] +
        #         ([{"address": {"email": mgr, "header_to": primary_to}} for mgr in cc_addresses] if cc_addresses else [])
        #     ),
        #     "content": {
        #         "from": "gre_reporting@comcast.com",
        #         "subject": f"SOP Request - Request ID: {request_id}",
        #         "text": build_text(is_manager=False),
        #         "html": build_html(is_manager=False),
        #         "headers": {
        #             "CC": ", ".join(cc_addresses) if cc_addresses else ""
        #         }
        #     }
        # }
        #
        # Current behavior: send only to the user (no CC)
        mdp_payload = {
            "recipients": [{"address": {"email": email}} for email in recipients],
            "content": {
                "from": "gre_reporting@comcast.com",
                "subject": f"SOP Request - Request ID: {request_id}",
                "text": build_text(is_manager=False),
                "html": build_html(is_manager=False)
            }
        }

        ok, _ = send_via_mdp_api_base(cfg["auth_user"], cfg["auth_pass"], cfg["mdp_api"], mdp_payload)
        if not ok:
            return False

        logging.info(f"✅ Email sent via MDP API to: {', '.join(recipients)} for Request ID {request_id}.")
        return True

    except Exception as e:
        logging.error(f"❌ Failed to send email via MDP API: {e}", exc_info=True)
        return False
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
        email_sent = send_email_notification(sop_data, request_id)
        if not email_sent:
            logging.warning(f"Email notification failed for request ID: {request_id}")
        
        return jsonify({
            "success": True, 
            "message": "SOP Request submitted successfully and saved to the database.",
            "request_id": request_id,
            "created_at": created_at,
            "email_sent": email_sent
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