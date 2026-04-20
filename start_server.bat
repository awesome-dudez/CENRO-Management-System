@echo off
echo Setting up CENRO Sanitary Management System...
echo.

echo Creating migrations...
python manage.py makemigrations accounts services scheduling dashboard

echo.
echo Running migrations...
python manage.py migrate

echo.
echo Starting development server...
echo Server will be available at http://127.0.0.1:8000/
echo Press Ctrl+C to stop the server
echo.
python manage.py runserver
