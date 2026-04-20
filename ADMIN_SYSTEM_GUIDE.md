# CENRO Sanitary Management System - Admin Section Implementation Guide

## Overview
A comprehensive Django-based management system for environmental services with role-based access control, service computation, membership tracking, and declogging applications.

## Features Implemented

### 1. Authentication & Authorization
- ✅ Login/Logout for all user types (Admin, Staff, Consumer)
- ✅ Role-based access control via `@role_required` decorator
- ✅ User approval workflow for staff and consumers
- ✅ Logout button in navigation bar for all authenticated users

### 2. Admin Dashboard
- ✅ Monthly service trends (incoming vs completed)
- ✅ Weekly analytics graphs
- ✅ Barangay ranking
- ✅ Service distribution (Residential/Commercial)
- ✅ Summary cards (Total Requests, Pending, Completed this month)
- ✅ Efficiency trend calculation

### 3. Service Requests Management
- ✅ Pending requests tab - view and process new requests
- ✅ Approved requests tab - requests with computed fees
- ✅ Schedule tab - scheduled services with barangay grouping
- ✅ Completed tab - historical completed services
- ✅ 10-person per barangay limit tracking
- ✅ Request approval workflow

### 4. Membership Management
- ✅ Member registration with consumer profiles (barangay, address, GPS)
- ✅ Account management interface
- ✅ Service history table with:
  - Service date
  - Cubic meters used
  - Remaining balance tracking
  - Payment status (Paid/Free)
  - Member information
- ✅ Printable service history reports

### 5. Service Computation & Charges
- ✅ Category-based charges:
  - Residential
  - Commercial
- ✅ Tipping fee: ₱500/m³
- ✅ Inspection fee: ₱150
- ✅ Outside Bayawan formula:
  - Distance × 2 × ₱20
  - Wear and tear: 20% of base cost
  - Meals/Transport allowance (configurable)
- ✅ Real-time computation form
- ✅ Receipt generation with:
  - Charges breakdown
  - "Prepared By" field
  - Printable format
- ✅ Payment status tracking (Pending, Paid, Free)

### 6. Declogging Application Module
- ✅ Application form with:
  - Applicant information
  - Applicant signature upload
  - CENRO representative assignment
  - CENRO signature upload
  - Date tracking for both parties
- ✅ Printable official letters with:
  - CENRO letterhead
  - Applicant and CENRO signature lines
  - Official approval stamp space

## Models Created

### dashboard/models.py
```
- ChargeCategory: Service charge categories (Residential/Commercial)
- ServiceComputation: Detailed computation with all charges
- DecloggingApplication: Declogging service applications with signatures
- MembershipRecord: Member balance and service history tracking
```

### services/models.py (Enhanced)
```
- ServiceRequest: Base service request model
- Notification: User notifications
```

### scheduling/models.py
```
- Schedule: Service scheduling with staff assignment and barangay tracking
```

## Views & URLs Implemented

### Admin Views (dashboard/admin_views.py)
- `admin_dashboard` - Main dashboard with analytics
- `admin_requests` - Request management with tabs
- `approve_request` - Approve pending requests
- `admin_schedule_by_barangay` - Schedule management
- `admin_membership` - Member management
- `member_service_history` - Member service history & balance
- `admin_computation` - Service charge computation
- `generate_receipt` - Receipt generation
- `admin_declogging_app` - Declogging application form

### URLs (dashboard/urls.py)
```python
path("", views.home, name="home"),
path("admin/", admin_views.admin_dashboard, name="admin_dashboard"),
path("admin/requests/", admin_views.admin_requests, name="admin_requests"),
path("admin/requests/approve/<int:pk>/", admin_views.approve_request, name="approve_request"),
path("admin/requests/schedule/", admin_views.admin_schedule_by_barangay, name="admin_schedule"),
path("admin/membership/", admin_views.admin_membership, name="admin_membership"),
path("admin/membership/history/<int:user_id>/", admin_views.member_service_history, name="member_service_history"),
path("admin/computation/", admin_views.admin_computation, name="admin_computation"),
path("admin/computation/generate-receipt/", admin_views.generate_receipt, name="generate_receipt"),
path("admin/declogging-app/", admin_views.admin_declogging_app, name="admin_declogging_app"),
```

## Forms Created (dashboard/forms.py)

- `ServiceComputationForm` - For detailed service computation
- `DecloggingApplicationForm` - For declogging applications
- `QuickComputationForm` - For quick on-the-fly calculations
- `MembershipSearchForm` - For member search and filtering

## Templates

### Admin Dashboard
- `dashboard/admin_dashboard.html` - Main dashboard with charts
- `dashboard/admin_requests.html` - Request management interface
- `dashboard/admin_schedule.html` - Schedule management
- `dashboard/admin_membership.html` - Membership management
- `dashboard/member_service_history.html` - Service history report
- `dashboard/admin_computation.html` - Computation calculator
- `dashboard/admin_declogging_app.html` - Declogging application form

### Base Template
- `base.html` - Updated with logout button for all users

## Database Schema

### ChargeCategory
- id (PK)
- category (RESIDENTIAL, COMMERCIAL)
- base_rate (Decimal)
- description (Text)
- created_at, updated_at (DateTime)

### ServiceComputation
- id (PK)
- service_request (FK to ServiceRequest)
- charge_category (FK to ChargeCategory)
- cubic_meters (Decimal)
- base_charge, distance_charge, wear_charge (Decimal)
- meals_transport_charge, tipping_charge, inspection_charge (Decimal)
- total_charge (Decimal)
- payment_status (PENDING, PAID, FREE)
- prepared_by (FK to User)
- receipt_generated (Boolean)
- receipt_date (DateTime)
- created_at, updated_at (DateTime)

### DecloggingApplication
- id (PK)
- service_request (FK to ServiceRequest)
- applicant_name (CharField)
- applicant_signature (FileField)
- applicant_sign_date (DateField)
- cenro_representative (FK to User)
- cenro_signature (FileField)
- cenro_sign_date (DateField)
- is_signed (Boolean)
- application_date, updated_at (DateTime)

### MembershipRecord
- id (PK)
- user (OneToOne FK to User)
- total_paid, total_free, remaining_balance (Decimal)
- is_active (Boolean)
- joined_date (DateField)
- created_at, updated_at (DateTime)

## Setup Instructions

### 1. Create Migrations
```bash
python manage.py makemigrations dashboard
python manage.py migrate
```

### 2. Create Initial Data
```bash
python manage.py shell
```

```python
from dashboard.models import ChargeCategory

# Create charge categories
ChargeCategory.objects.create(
    category='RESIDENTIAL',
    base_rate=100.00,
    description='Residential property service charges'
)

ChargeCategory.objects.create(
    category='COMMERCIAL',
    base_rate=150.00,
    description='Commercial property service charges'
)
```

### 3. Admin Portal Access
- URL: `http://127.0.0.1:8000/dashboard/admin/`
- Login with admin account
- Use role-based navigation to access:
  - Dashboard - Analytics and overview
  - Requests - Manage service requests
  - Membership - Manage members
  - Computation - Calculate service charges
  - Declogging App - Process declogging applications

## Usage Examples

### Computing Service Charges
1. Go to "Computation" section
2. Select category (Residential/Commercial)
3. Enter cubic meters
4. If outside Bayawan, enter distance
5. System automatically calculates:
   - Tipping fee (₱500/m³)
   - Inspection fee (₱150)
   - Distance charges if applicable
   - Wear and tear (20%)
6. View breakdown and print receipt

### Processing Declogging Application
1. Go to "Declogging App" section
2. Enter applicant name and date
3. Upload applicant signature
4. Select CENRO representative
5. Upload representative signature
6. Print official letter with signatures
7. Save and archive

### Managing Member Service History
1. Go to "Membership" section
2. Select "Service History"
3. Choose a member
4. View complete service history:
   - Dates of service
   - Cubic meters used
   - Payment status
   - Remaining balance
5. Generate and print detailed report

## Security Features

- ✅ Role-based access control (@role_required decorator)
- ✅ Login required for all admin views (@login_required)
- ✅ CSRF protection on all forms
- ✅ User approval workflow
- ✅ Audit trail (created_at, updated_at timestamps)
- ✅ File upload security (designated upload directories)

## Printing & Reports

All sections include printable views:
- Service computation receipts
- Member service history reports
- Declogging application forms
- Admin dashboard summaries

Use browser print function (Ctrl+P) for professional reports.

## Database Compatibility

- ✅ MySQL compatible
- ✅ SQLite compatible (development)
- Uses standard Django ORM for portability

## Extensibility

Future enhancements:
- PDF report generation
- Email receipt sending
- Payment gateway integration
- Advanced analytics and dashboards
- SMS notifications
- Mobile app integration
- Barcode/QR code receipt tracking

## Troubleshooting

### Migrations not found
```bash
python manage.py makemigrations dashboard --empty dashboard --name init
```

### Admin portal showing 404
- Verify dashboard app is in INSTALLED_APPS
- Check URLs are properly configured
- Ensure user has admin role: `user.role = User.Role.ADMIN`

### Computation not calculating
- Verify ChargeCategory objects exist in database
- Check decimal values are properly formatted
- Ensure cubic_meters field is filled

## Support
For issues or questions, refer to the Django documentation and CENRO management guidelines.
