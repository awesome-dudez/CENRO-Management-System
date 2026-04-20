# Admin Section Implementation Guide

## Overview
The admin section has been redesigned with a modern, clean interface featuring top navigation instead of a sidebar.

## Admin Features Implemented

### 1. Main Dashboard (`/admin/`)
- **Summary Cards**: Total Requests, Pending Service, Completed (Monthly), Active Trucks
- **Service Request Trends Chart**: Weekly bar chart showing incoming requests vs completions
- **Service Distribution Chart**: Percentage breakdown by service type (Residential/Commercial)
- **Barangay Ranking Table**: Top barangays ranked by total service requests with percentage bars

### 2. Service Request Tab (`/admin/requests/`)
Four sub-tabs:
- **Pending Requests**: Shows requests awaiting approval with "Approve" buttons
- **Approved**: Shows approved requests queuing for service
- **Schedule**: Displays customers scheduled per barangay with 10-person limit tracking
- **Completed**: Shows finalized service records with cubic measurements and completion dates

### 3. Membership Tab (`/admin/membership/`)
Three sub-tabs:
- **Registration**: Form to register new members with receipt upload
- **Account Management**: Table of all members with location classification
- **Service History**: View service history for members with remaining balance tracking

### 4. Computation Tab (`/admin/computation/`)
- **Category Selection**: Residential or Commercial
- **Location Selection**: Inside Bayawan or Outside Bayawan City
- **Cost Calculation**:
  - Tipping Fee: ₱500.00 per cubic meter
  - Inspection Fee: ₱150.00
  - Outside Bayawan: Distance × 2 × ₱20.00 + Wear & Tear (20%) + Meals & Transport (₱500)
- **Receipt Generation**: Printable receipt for Treasurer's Office

### 5. Declogging Services Application Tab (`/admin/declogging-app/`)
- Formal application form template
- Print functionality for official documents
- Signature fields for applicant and CENRO personnel

## Key Features

### 10-Person Limit Tracking
The Schedule view tracks progress toward a 10-person limit per barangay:
- Shows count (e.g., "7 of 10 customers")
- Visual progress bar with percentage
- Status: "Awaiting Capacity" or "Ready for Deployment"

### 4-Year Cycle Tracking
- Tracks remaining balance (limited to 5 cubic meters every 4 years)
- Calculates total used vs. quota
- Displays in member service history

### Report Generation
- Print functionality for service history reports
- Export data functionality (placeholder for future implementation)
- Printable receipts and application forms

## Design Elements

### Color Scheme
- Primary Green: #2d8659
- Secondary Green: #4a9d7a
- Sky Blue: #5b9bd5
- Orange: #f39c12 (for warnings/pending)

### Layout
- Top navigation bar with CENRO Sanitary Management System branding
- Horizontal admin tabs (Dashboard, Requests, Membership, Computation, Declogging App)
- Secondary navigation tabs for sub-sections
- Card-based layouts throughout
- Responsive design (desktop-first)

## Access Control

All admin views are protected with:
- `@login_required` decorator
- `@role_required("ADMIN")` decorator

Admins are automatically redirected to `/admin/` when accessing the main dashboard.

## Next Steps

1. **Run Migrations**: Create and apply migrations for the new `cubic_meters` field:
   ```bash
   python manage.py makemigrations
   python manage.py migrate
   ```

2. **Set Admin Role**: Ensure your superuser has `role=ADMIN`:
   ```python
   from accounts.models import User
   admin = User.objects.get(username='your_username')
   admin.role = User.Role.ADMIN
   admin.is_approved = True
   admin.save()
   ```

3. **Test Features**:
   - Create test service requests
   - Test approval workflow
   - Test cost computation
   - Test schedule tracking
   - Test report generation

## Notes

- Chart.js is loaded via CDN for dashboard charts
- Print functionality uses browser's native print dialog
- Export functionality is a placeholder and can be enhanced with PDF generation libraries
- The 10-person limit logic is implemented in the schedule view
- Cubic meters tracking requires the new migration to be applied
