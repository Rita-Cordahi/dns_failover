import requests
import bs4
import pyotp
import time
import sys
import os

def renew():
    username = os.getenv("PA_USERNAME", "JHFGUF")
    password = os.getenv("PA_PASSWORD", "JHGjhf5475%^")
    totp_secret = os.getenv("PA_TOTP_SECRET", "4RQLUKK6XN62I4OH3DTXMORWVABDRZS6")
    domain = os.getenv("PA_DOMAIN", "jhfguf.pythonanywhere.com")

    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.pythonanywhere.com/login/'
    })

    print("Fetching login page...")
    r = s.get('https://www.pythonanywhere.com/login/')
    soup = bs4.BeautifulSoup(r.text, 'html.parser')
    csrf_elem = soup.find('input', {'name': 'csrfmiddlewaretoken'})
    if not csrf_elem:
        print("Failed to find CSRF token on login page.")
        sys.exit(1)
    csrf = csrf_elem.get('value')

    time_left = 30 - (time.time() % 30)
    if time_left < 3:
        print(f"Only {time_left:.1f}s left in OTP window. Waiting for new window...")
        time.sleep(time_left + 1)

    otp = pyotp.TOTP(totp_secret).now()
    print(f"Submitting credentials (OTP: {otp})...")
    data = {
        'csrfmiddlewaretoken': csrf,
        'login_view-current_step': 'auth',
        'auth-username': username,
        'auth-password': password
    }
    r2 = s.post('https://www.pythonanywhere.com/login/', data=data)
    
    if "Please enter the token" not in r2.text:
        print("Failed to trigger 2FA page. Checking if already logged in...")
        if "Logout" in r2.text:
            print("Already logged in!")
        else:
            print("Login step 1 failed.")
            sys.exit(1)
    else:
        soup2 = bs4.BeautifulSoup(r2.text, 'html.parser')
        csrf2 = soup2.find('input', {'name': 'csrfmiddlewaretoken'}).get('value')
        data2 = {
            'csrfmiddlewaretoken': csrf2,
            'login_view-current_step': 'token',
            'token-otp_token': otp
        }
        print("Submitting 2FA token...")
        r3 = s.post('https://www.pythonanywhere.com/login/', data=data2)
        if "Login" in bs4.BeautifulSoup(r3.text, 'html.parser').title.text:
            print("2FA verification failed. Retrying in next window...")
            time.sleep(30 - (time.time() % 30) + 1)
            otp = pyotp.TOTP(totp_secret).now()
            data2['token-otp_token'] = otp
            r3 = s.post('https://www.pythonanywhere.com/login/', data=data2)

    r_dashboard = s.get(f'https://www.pythonanywhere.com/user/{username}/webapps/')
    if "Login" in bs4.BeautifulSoup(r_dashboard.text, 'html.parser').title.text:
        print("Login failed completely.")
        sys.exit(1)

    print("Logged in successfully! Extending web app...")
    extend_url = f"https://www.pythonanywhere.com/user/{username}/webapps/{domain}/extend"
    csrf_webapp = s.cookies.get('csrftoken', domain='www.pythonanywhere.com')
    headers = {
        'X-CSRFToken': csrf_webapp,
        'Referer': f'https://www.pythonanywhere.com/user/{username}/webapps/'
    }
    r_extend = s.post(extend_url, headers=headers, data={'csrfmiddlewaretoken': csrf_webapp})
    print(f"Extend status code: {r_extend.status_code}")
    print(f"Extend response content: {r_extend.text[:500]}")
    if r_extend.status_code == 200:
        print("Webapp successfully extended for 1 month!")
    else:
        print("Failed to extend webapp.")

if __name__ == '__main__':
    renew()
