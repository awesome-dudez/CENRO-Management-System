# CENRO System - Login Guide for Users

## 🔐 Login Requirement

**Important**: You must login to use the CENRO Sanitary Management System. All protected content requires authentication.

---

## Step-by-Step Login Instructions

### Step 1: Access Login Page
1. Open your web browser
2. Go to: `http://127.0.0.1:8000/accounts/login/`
   OR
   Click "Login" button on any public page

### Step 2: Enter Your Credentials
1. **Username**: Enter your username (created during registration)
2. **Password**: Enter your password (case-sensitive)
3. Make sure CAPS LOCK is OFF

### Step 3: Submit Login
1. Click the **"Login"** button
2. Wait for page to process (usually 1-2 seconds)

### Step 4: Verify Success
1. If successful:
   - You'll be redirected to your dashboard
   - Your username will appear in top-right corner
   - Logout button will be visible

2. If unsuccessful:
   - Error message will display
   - Stay on login page to retry
   - Check username/password spelling

---

## What Happens After Login?

### For Consumers
- Redirected to **User Dashboard**
- Can create service requests
- Can track request status
- Can view service history

### For Staff
- Redirected to **User Dashboard**
- Can view assigned schedules
- Can access client information
- Can complete service records

### For Admins
- Redirected to **Admin Dashboard**
- Full access to all admin features
- Can approve requests
- Can manage members
- Can generate reports

---

## Troubleshooting Login Issues

### "Invalid Username or Password"
**Possible causes**:
- Username typed incorrectly
- Password typed incorrectly
- CAPS LOCK is ON
- Spaces before/after username

**Solution**:
- Double-check spelling
- Make sure CAPS LOCK is OFF
- Copy-paste from confirmation email if available
- Contact admin for password reset

---

### "Your Account is Pending Approval"
**Cause**: New accounts need admin approval

**Solution**:
- Wait for admin to approve your account
- Check email for approval confirmation
- Contact admin to check status

---

### "Page Not Found" on Login
**Cause**: Browser or network issue

**Solution**:
1. Refresh page (F5 or Cmd+R)
2. Clear browser cache (Ctrl+Shift+Delete)
3. Try different browser
4. Check internet connection

---

### "This Page Can't Be Reached"
**Cause**: Server is down or unreachable

**Solution**:
1. Wait a few minutes
2. Contact system administrator
3. Check if server is running

---

## If You Don't Have an Account

### For Consumers
1. Go to: `http://127.0.0.1:8000/accounts/register/consumer/`
2. Fill out registration form
3. Submit registration
4. Wait for admin approval
5. Login once approved

### For Staff
1. Contact admin to create staff account
2. Wait for account creation and approval
3. Receive temporary password
4. Login and change password

---

## After Logging In

### Top Navigation Bar Shows:
- 👤 Your username
- **Logout** button (click to logout)
- Role-specific navigation links

### Available Features
**Consumers can**:
- 📝 Create service requests
- 📊 Track request status
- 📋 View service history

**Staff can**:
- 📅 View assigned schedules
- 📋 View client records
- ✅ Mark services complete

**Admins can**:
- ⚙️ Access admin features
- 👥 Manage members
- 💰 Compute charges
- 📄 Generate documents

---

## Logging Out

### To Logout:
1. Click **"Logout"** button in top-right corner
2. You'll be redirected to home page
3. All session data cleared
4. Must login again to access protected pages

### Important
- Always logout on shared computers
- Your session is automatically cleared after 2 weeks of inactivity
- Closing browser does NOT logout (unless configured)

---

## Password Tips

### Good Password Practices
- ✅ Use mix of uppercase and lowercase
- ✅ Include numbers and symbols
- ✅ At least 8 characters long
- ✅ Don't share your password
- ✅ Change password regularly

### Bad Passwords
- ❌ Birth date (1990)
- ❌ Simple words (password, admin)
- ❌ Dictionary words
- ❌ Sequences (123456, abcdef)
- ❌ Same as username

---

## Security Reminders

- 🔐 Never share your password with anyone
- 🔐 Don't enter password on public WiFi (use VPN)
- 🔐 Logout when leaving shared computer
- 🔐 Report suspicious activity to admin
- 🔐 Change password if compromised

---

## Quick Links

| Page | URL |
|------|-----|
| Login | `/accounts/login/` |
| Register (Consumer) | `/accounts/register/consumer/` |
| Register (Staff) | `/accounts/register/staff/` |
| User Dashboard | `/` |
| Admin Dashboard | `/admin/` |
| Request Service | `/services/create_request/` |
| Track Status | `/services/request_list/` |

---

## Getting Help

If you can't login:
1. Try password reset (if available)
2. Contact your administrator
3. Check system status
4. Verify account was approved

---

**Remember**: Always login with your personal credentials. Never share your password!

Last Updated: January 29, 2026
