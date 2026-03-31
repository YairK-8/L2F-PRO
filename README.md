# L2F — Warehouse Management System

מערכת ניהול חוסרים ומיקומים למחסן/חנות בגדים.  
Flask + SQLite | עובדת ברשת מקומית מכמה מכשירים.

---

## מבנה הפרויקט

```
l2f/
├── app.py                  # נקודת כניסה — הרצת השרת
├── requirements.txt
├── backend/
│   ├── barcodes.py         # API קטלוג ברקודים
│   ├── missing_floor.py    # API לשונית 1 — לא נסרק = חסר
│   ├── missing_warehouse.py# API לשונית 2 — נסרק = חסר
│   └── locations.py        # API לשונית 3 — מיקומים במחסן
├── database/
│   ├── db.py               # חיבור + אתחול SQLite
│   ├── schema.sql          # הגדרת הטבלאות
│   └── l2f.db              # נוצר אוטומטית בהרצה ראשונה
├── templates/
│   └── index.html          # ה-Frontend (RTL, עברית)
└── static/                 # CSS/JS נוספים בעתיד
```

---

## הרצה ראשונה

### 1. התקן Python 3.9+

ודא שמותקן:
```bash
python --version
```

### 2. צור סביבת וירטואלי (מומלץ)

```bash
cd l2f
python -m venv venv

# Windows:
venv\Scripts\activate

# Mac/Linux:
source venv/bin/activate
```

### 3. התקן dependencies

```bash
pip install -r requirements.txt
```

### 4. הרץ

```bash
python app.py
```

תראה:
```
🚀 L2F Server running at http://0.0.0.0:5000
   Open on this machine:  http://localhost:5000
   Open on other devices: http://<YOUR-IP>:5000
```

### 5. גישה מהטלפון/טאבלט

גלה מה ה-IP של המחשב שלך ברשת:

- **Windows**: `ipconfig` → "IPv4 Address"
- **Mac/Linux**: `ifconfig` → `inet` של הכרטיס הרשתי

ואז פתח בדפדפן: `http://192.168.x.x:5000`

---

## API Endpoints

### ברקודים
| Method | URL | תיאור |
|--------|-----|-------|
| GET | `/api/barcodes` | כל הברקודים |
| GET | `/api/barcodes/<barcode>` | ברקוד ספציפי |
| POST | `/api/barcodes` | הוספת ברקוד |
| DELETE | `/api/barcodes/<barcode>` | מחיקת ברקוד |
| POST | `/api/barcodes/import` | ייבוא CSV |

### לשונית 1 — חוסרים מהרצפה
| Method | URL | תיאור |
|--------|-----|-------|
| GET | `/api/missing-floor` | רשימת חוסרים פעילים |
| POST | `/api/missing-floor` | הוספת חוסר ידנית |
| POST | `/api/missing-floor/scan` | סריקת ברקוד בוקר |
| POST | `/api/missing-floor/<id>/resolve` | סימון כנמצא |
| POST | `/api/missing-floor/clear` | נקה הכל |

### לשונית 2 — חוסרים מחסן
| Method | URL | תיאור |
|--------|-----|-------|
| GET | `/api/missing-warehouse` | רשימת ממתינים (FIFO) |
| POST | `/api/missing-warehouse/scan` | סריקת פריט שנמכר |
| POST | `/api/missing-warehouse/<id>/restock` | סימון כהושלם |
| POST | `/api/missing-warehouse/clear` | נקה ממתינים |

### לשונית 3 — מיקומים
| Method | URL | תיאור |
|--------|-----|-------|
| GET | `/api/locations` | כל המיקומים |
| GET | `/api/locations/search?sku=...` | חיפוש לפי מק״ט |
| POST | `/api/locations` | הוספה/עדכון (upsert) |
| DELETE | `/api/locations/<sku>` | מחיקה |
| GET | `/api/locations/export/json` | ייצוא JSON |
| POST | `/api/locations/import/json` | ייבוא JSON |

---

## ייבוא קטלוג ברקודים

עמודות CSV נדרשות: `barcode,sku,color,size`

דוגמה:
```csv
barcode,sku,color,size
1234567890,A123,שחור,m
1234567891,A123,שחור,l
```

---

## המלצות להמשך

1. **Gunicorn** — להרצה יציבה יותר במקום `debug=True`
2. **אימות משתמשים** — Flask-Login אם נדרשת הגבלת גישה
3. **WebSockets** — עדכונים בזמן אמת בין מכשירים (Flask-SocketIO)
4. **גיבוי אוטומטי** — cron job שמעתיק את `l2f.db` כל יום
5. **HTTPS** — עם nginx + Let's Encrypt אם תעבור לאינטרנט
