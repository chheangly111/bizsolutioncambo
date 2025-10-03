import os
import time
import uuid
from functools import wraps
import io
from urllib.request import urlopen
from datetime import datetime, timedelta
import json

import firebase_admin
from firebase_admin import auth, credentials, firestore, storage
from flask import Flask, jsonify, render_template, request, send_file, make_response

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Image
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from PIL import Image as PILImage
from google.cloud.firestore_v1.base_query import FieldFilter

# --- Firebase Initialization ---
cred_path = os.path.join(os.path.dirname(__file__), 'firebase_credentials.json')
cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred, {
    'storageBucket': 'posbyhcl.firebasestorage.app'
})
db = firestore.client()
bucket = storage.bucket()

app = Flask(__name__)

# --- Authentication Decorator ---
def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        id_token = request.headers.get('Authorization', '').split('Bearer ')[-1]
        if not id_token:
            return jsonify({"success": False, "message": "Authorization token required."}), 401
        try:
            decoded_token = auth.verify_id_token(id_token)
            request.user_id = decoded_token['uid']
        except Exception as e:
            return jsonify({"success": False, "message": f"Invalid token: {e}"}), 401
        return f(*args, **kwargs)
    return decorated_function

# --- Frontend Route ---
@app.route('/')
def index():
    return render_template('index.html')

# --- Store Settings Endpoints ---
@app.route('/api/store/settings', methods=['GET'])
@require_auth
def get_store_settings():
    """Fetches store settings for the authenticated user."""
    try:
        uid = request.user_id
        settings_ref = db.collection('users').document(uid).collection('settings').document('store')
        settings_doc = settings_ref.get()
        if settings_doc.exists:
            return jsonify({"success": True, "settings": settings_doc.to_dict()})
        return jsonify({"success": True, "settings": {}})
    except Exception as e:
        return jsonify({"success": False, "message": f"Error fetching settings: {e}"}), 500

@app.route('/api/store/settings', methods=['POST'])
@require_auth
def update_store_settings():
    """Updates store settings for the authenticated user."""
    try:
        uid = request.user_id
        data = request.json
        settings_ref = db.collection('users').document(uid).collection('settings').document('store')
        settings_ref.set(data, merge=True)
        return jsonify({"success": True, "message": "Store information updated successfully."})
    except Exception as e:
        return jsonify({"success": False, "message": f"Error updating settings: {e}"}), 500

# --- Public Storefront Endpoints ---
@app.route('/api/store/<user_id>', methods=['GET'])
def get_store_products(user_id):
    """
    Public endpoint to fetch all products and store settings for a given user.
    """
    try:
        settings_ref = db.collection('users').document(user_id).collection('settings').document('store')
        settings_doc = settings_ref.get()
        settings_data = settings_doc.to_dict() if settings_doc.exists else {}
        
        products_query = db.collection('users').document(user_id).collection('products').order_by('item_number')
        all_docs = products_query.stream()
        products = [doc.to_dict() for doc in all_docs]
        
        response_data = { "success": True, "settings": settings_data, "products": products }
        
        response = make_response(jsonify(response_data))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        
        return response
    except Exception as e:
        error_response = make_response(jsonify({"success": False, "message": f"Could not retrieve store data: {e}"}))
        error_response.status_code = 404
        return error_response

@app.route('/store/<user_id>')
def serve_store_page(user_id):
    """
    Serves the static HTML page for the customer storefront.
    """
    return render_template('store.html')

# --- Product API Endpoints ---
@app.route('/api/products', methods=['GET'])
@require_auth
def get_products():
    try:
        uid = request.user_id
        products_query = db.collection('users').document(uid).collection('products').order_by('item_number')
        
        limit_str = request.args.get('limit')
        if limit_str:
            limit = int(limit_str)
            start_after = request.args.get('start_after', None)
            if start_after:
                start_doc = db.collection('users').document(uid).collection('products').document(start_after).get()
                products_query = products_query.start_after(start_doc)
            
            products_docs = products_query.limit(limit).stream()
            products = [doc.to_dict() for doc in products_docs]
            has_next = len(products) == limit
            return jsonify({ "products": products, "has_next": has_next })
        else:
            all_docs = products_query.stream()
            products = [doc.to_dict() for doc in all_docs]
            return jsonify({ "products": products, "has_next": False })
    except Exception as e:
        return jsonify({"success": False, "message": f"Error getting products: {e}"}), 500

@app.route('/api/products', methods=['POST'])
@require_auth
def add_update_product():
    """
    Handles adding and updating products, now with multiple image support.
    """
    try:
        uid = request.user_id
        product_data = request.form.to_dict()
        item_number = product_data.get('item_number', '').upper()
        if not item_number: return jsonify({"success": False, "message": "Item Number is required."}), 400
        
        product_ref = db.collection('users').document(uid).collection('products').document(item_number)
        
        # --- NEW: MULTI-IMAGE HANDLING LOGIC ---
        
        # 1. Get existing image URLs from form (images user wants to keep)
        existing_urls_to_keep = json.loads(request.form.get('existing_image_urls', '[]'))
        
        # 2. Get image URLs currently in the database to compare
        existing_product_doc = product_ref.get()
        urls_in_db = []
        if existing_product_doc.exists:
            urls_in_db = existing_product_doc.to_dict().get('image_urls', [])

        # 3. Determine which images were removed by the user and delete from storage
        urls_to_delete = [url for url in urls_in_db if url not in existing_urls_to_keep]
        for url in urls_to_delete:
            try:
                # This logic extracts the file path from the public URL to delete it from storage
                if "firebasestorage.googleapis.com" in url:
                    start = url.find("/o/") + 3
                    end = url.find("?alt=media")
                    filename = unquote(url[start:end])
                    blob = bucket.blob(filename)
                    if blob.exists():
                        blob.delete()
            except Exception as e:
                app.logger.error(f"Could not delete file for URL {url}: {e}") # Log error but continue

        # 4. Upload any new images
        newly_uploaded_urls = []
        if 'images' in request.files:
            images = request.files.getlist('images')
            for image in images:
                if image.filename != '':
                    filename = f"products/{uid}/{item_number}_{uuid.uuid4()}"
                    blob = bucket.blob(filename)
                    blob.upload_from_file(image, content_type=image.content_type)
                    blob.make_public()
                    newly_uploaded_urls.append(blob.public_url)
        
        # 5. Combine kept and new URLs for the final list
        final_image_urls = existing_urls_to_keep + newly_uploaded_urls

        product_data['image_urls'] = final_image_urls
        # Also update the old single `image_url` for backward compatibility (e.g., in PDF reports)
        product_data['image_url'] = final_image_urls[0] if final_image_urls else ''

        # --- END OF MULTI-IMAGE LOGIC ---

        product_data['item_number'] = item_number
        product_ref.set(product_data, merge=True)
        return jsonify({"success": True, "message": "Product saved successfully."})
    except Exception as e:
        app.logger.error(f"Error saving product: {e}")
        return jsonify({"success": False, "message": f"Error saving product: {e}"}), 500


@app.route('/api/products/<item_number>', methods=['DELETE'])
@require_auth
def delete_product(item_number):
    try:
        uid = request.user_id
        item_number_upper = item_number.upper()
        
        product_ref = db.collection('users').document(uid).collection('products').document(item_number_upper)
        product_doc = product_ref.get()
        
        if product_doc.exists:
            # Delete associated images from storage
            image_urls = product_doc.to_dict().get('image_urls', [])
            for url in image_urls:
                try:
                    if "firebasestorage.googleapis.com" in url:
                        start = url.find("/o/") + 3
                        end = url.find("?alt=media")
                        filename = unquote(url[start:end])
                        blob = bucket.blob(filename)
                        if blob.exists():
                            blob.delete()
                except Exception as e:
                    app.logger.error(f"Could not delete file for product {item_number_upper}: {e}")

        # Transaction to delete product and sales records
        transaction = db.transaction()
        @firestore.transactional
        def delete_product_and_sales_transaction(transaction):
            sales_query = db.collection('users').document(uid).collection('sales').where(filter=FieldFilter('items', 'array_contains', {'item_number': item_number_upper}))
            sales_docs = sales_query.stream(transaction=transaction)
            for sale in sales_docs:
                transaction.delete(sale.reference)
            transaction.delete(product_ref)

        delete_product_and_sales_transaction(transaction)
        
        return jsonify({"success": True, "message": "Product and associated sales deleted."})
    except Exception as e:
        return jsonify({"success": False, "message": f"Error deleting product: {e}"}), 500

# --- Sales API Endpoints ---
@app.route('/api/sales', methods=['GET'])
@require_auth
def get_sales():
    try:
        uid = request.user_id
        sales_query = db.collection('users').document(uid).collection('sales')
        
        date_filter = request.args.get('date')
        month_filter = request.args.get('month')

        if date_filter:
            start_dt = datetime.strptime(date_filter, '%Y-%m-%d')
            end_dt = start_dt + timedelta(days=1)
            sales_query = sales_query.where(filter=FieldFilter('timestamp', '>=', start_dt.timestamp())).where(filter=FieldFilter('timestamp', '<', end_dt.timestamp()))
        elif month_filter:
            start_dt = datetime.strptime(month_filter, '%Y-%m')
            next_month_start_year = start_dt.year + 1 if start_dt.month == 12 else start_dt.year
            next_month_start_month = 1 if start_dt.month == 12 else start_dt.month + 1
            end_dt = datetime(next_month_start_year, next_month_start_month, 1)
            sales_query = sales_query.where(filter=FieldFilter('timestamp', '>=', start_dt.timestamp())).where(filter=FieldFilter('timestamp', '<', end_dt.timestamp()))

        sales_ref = sales_query.order_by('timestamp', direction=firestore.Query.DESCENDING).stream()
        
        sales_list = []
        total_profit = 0
        
        for sale in sales_ref:
            sale_data = sale.to_dict()
            sale_data['id'] = sale.id
            sales_list.append(sale_data)
            
            for item in sale_data.get('items', []):
                profit_per_item = float(item.get('selling_price', 0)) - float(item.get('import_price', 0))
                total_profit += profit_per_item * int(item.get('quantity', 0))

        return jsonify({"sales": sales_list, "total_profit": total_profit})

    except Exception as e:
        return jsonify({"success": False, "message": f"Error getting sales: {e}"}), 500

@app.route('/api/sales', methods=['POST'])
@require_auth
def record_sale():
    try:
        uid = request.user_id
        sale_items = request.json.get('items', [])
        if not sale_items:
            return jsonify({"success": False, "message": "Sale must contain at least one item."}), 400

        transaction = db.transaction()
        @firestore.transactional
        def update_in_transaction(transaction, sale_items_payload):
            product_refs_to_update = {}
            
            for item in sale_items_payload:
                item_number = item.get('item_number', '').upper()
                product_ref = db.collection('users').document(uid).collection('products').document(item_number)
                snapshot = product_ref.get(transaction=transaction)
                if not snapshot.exists:
                    raise ValueError(f"Product {item_number} not found.")
                
                product_data = snapshot.to_dict()
                current_quantity = int(product_data.get('quantity', 0))
                quantity_sold = int(item.get('quantity', 0))
                if current_quantity < quantity_sold:
                    raise ValueError(f"Not enough stock for {product_data.get('item_name')}.")
                
                new_quantity = current_quantity - quantity_sold
                product_refs_to_update[item_number] = (product_ref, new_quantity)
                item['import_price'] = float(product_data.get('import_price', 0))

            total_sale_amount = 0
            for item in sale_items_payload:
                item_number = item.get('item_number').upper()
                product_ref, new_quantity = product_refs_to_update[item_number]
                transaction.update(product_ref, {'quantity': new_quantity})
                total_sale_amount += float(item.get('selling_price')) * int(item.get('quantity'))
            
            sale_ref = db.collection('users').document(uid).collection('sales').document()
            sale_data = { "items": sale_items_payload, "total_amount": total_sale_amount, "timestamp": time.time() }
            transaction.set(sale_ref, sale_data)

        update_in_transaction(transaction, sale_items)
        
        return jsonify({"success": True, "message": "Sale recorded successfully!"})
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

@app.route('/api/sales/<sale_id>', methods=['DELETE'])
@require_auth
def delete_sale(sale_id):
    try:
        uid = request.user_id
        sale_ref = db.collection('users').document(uid).collection('sales').document(sale_id)

        transaction = db.transaction()
        @firestore.transactional
        def restore_stock_and_delete_sale(transaction):
            sale_doc = sale_ref.get(transaction=transaction)
            if not sale_doc.exists: raise ValueError("Sale record not found.")
            
            sale_data = sale_doc.to_dict()
            for item in sale_data.get('items', []):
                item_number = item.get('item_number')
                quantity_restored = int(item.get('quantity', 0))
                if item_number and quantity_restored > 0:
                    product_ref = db.collection('users').document(uid).collection('products').document(item_number)
                    transaction.update(product_ref, {'quantity': firestore.Increment(quantity_restored)})
            
            transaction.delete(sale_ref)
        
        restore_stock_and_delete_sale(transaction)
        return jsonify({"success": True, "message": "Sale deleted and stock restored."})
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 404
    except Exception as e:
        return jsonify({"success": False, "message": f"An unexpected error occurred: {e}"}), 500

# --- PDF Generation Endpoint ---
def get_image_for_pdf(url, width=50):
    if not url: return "N/A"
    try:
        f = urlopen(url)
        img = PILImage.open(f)
        aspect = img.height / float(img.width)
        return Image(urlopen(url), width=width, height=width * aspect)
    except Exception: return "No Image"

@app.route('/api/generate-pdf', methods=['GET'])
@require_auth
def generate_stock_pdf():
    try:
        uid = request.user_id
        products_ref = db.collection('users').document(uid).collection('products').order_by('item_number').stream()
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        elements = []
        styles = getSampleStyleSheet()
        elements.append(Paragraph("Stock & Inventory Report", styles['h1']))
        elements.append(Paragraph(f"Report generated on: {time.strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']))
        table_data = [["Image", "Item #", "Name", "Qty", "Import Price", "Selling Price"]]
        
        for p in products_ref:
            product = p.to_dict()
            # Use the first image from the list for the PDF
            first_image = product.get('image_urls', [None])[0]
            table_data.append([
                get_image_for_pdf(first_image),
                product.get('item_number', 'N/A'),
                product.get('item_name', 'N/A'),
                str(product.get('quantity', 0)),
                f"${float(product.get('import_price', 0)):.2f}",
                f"${float(product.get('selling_price', 0)):.2f}"
            ])
            
        if len(table_data) > 1:
            table = Table(table_data, colWidths=[60, 80, 140, 40, 80, 80])
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey), ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'), ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige), ('GRID', (0, 0), (-1, -1), 1, colors.black)
            ]))
            elements.append(table)
        else:
            elements.append(Paragraph("No products in inventory.", styles['Normal']))
            
        doc.build(elements)
        buffer.seek(0)
        return send_file(buffer, as_attachment=True, download_name='stock_report.pdf', mimetype='application/pdf')
    except Exception as e:
        return jsonify({"success": False, "message": f"Error generating PDF: {e}"}), 500

# --- Product Type Endpoints ---
@app.route('/api/types', methods=['GET'])
@require_auth
def get_types():
    try:
        uid = request.user_id
        types_ref = db.collection('users').document(uid).collection('product_types').stream()
        types_list = [{'id': doc.id, **doc.to_dict()} for doc in types_ref]
        return jsonify({"success": True, "types": types_list})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/types', methods=['POST'])
@require_auth
def add_type():
    try:
        uid = request.user_id
        type_name = request.json.get('name', '').strip()
        if not type_name:
            return jsonify({"success": False, "message": "Type name cannot be empty."}), 400
        
        existing_types_query = db.collection('users').document(uid).collection('product_types').where(filter=FieldFilter('name', '==', type_name)).limit(1).stream()
        if len(list(existing_types_query)) > 0:
            return jsonify({"success": False, "message": "This product type already exists."}), 409

        db.collection('users').document(uid).collection('product_types').add({'name': type_name})
        return jsonify({"success": True, "message": "Product type added."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/types/<type_id>', methods=['DELETE'])
@require_auth
def delete_type(type_id):
    try:
        uid = request.user_id
        db.collection('users').document(uid).collection('product_types').document(type_id).delete()
        return jsonify({"success": True, "message": "Product type deleted."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)

