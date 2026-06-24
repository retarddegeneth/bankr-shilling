import sqlite3
db = sqlite3.connect('/data/data/com.termux/files/home/bankr-shilling/bankr.db')
cur = db.cursor()
cur.execute('SELECT id FROM users WHERE lower(x_handle)=?', ('retarddegeneth',))
row = cur.fetchone()
if row:
    uid = row[0]
    cur.execute('DELETE FROM tweets WHERE user_id=?', (uid,))
    cur.execute('DELETE FROM users WHERE id=?', (uid,))
    db.commit()
    print('deleted user id', uid)
else:
    print('user not found')
