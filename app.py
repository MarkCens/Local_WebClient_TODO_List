import os
import sqlite3
import io
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file
import openpyxl
from datetime import datetime, timedelta
from chinese_calendar import get_holiday_detail
from zhdate import ZhDate


app = Flask(__name__)
DB_DIR = 'database'
DB_PATH = os.path.join(DB_DIR, 'todo.db')

if not os.path.exists(DB_DIR):
    os.makedirs(DB_DIR)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                   name TEXT NOT NULL,
                                                   date TEXT NOT NULL,
                                                   priority INTEGER NOT NULL,
                                                   is_completed INTEGER DEFAULT 0)''')
    try:
        c.execute('ALTER TABLE tasks ADD COLUMN sort_order INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    # 建立远期目标数据表
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS long_term_goals(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        is_completed INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                     )''')
    except sqlite3.OperationalError:
        pass

    try:
        c.execute('ALTER TABLE long_term_goals ADD COLUMN is_principal_contradiction INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


init_db()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/meta', methods=['GET'])
def get_meta():
    conn = get_db()
    row = conn.execute(
        'SELECT MIN(SUBSTR(date, 1, 4)) as min_year FROM tasks WHERE date IS NOT NULL AND date != ""').fetchone()
    conn.close()
    current_year = datetime.now().year
    min_y = int(row['min_year']) if row['min_year'] else current_year
    return jsonify({'min_year': min_y, 'current_year': current_year})


@app.route('/api/tasks', methods=['GET'])
def get_tasks():
    year = request.args.get('year', datetime.now().strftime('%Y'))
    conn = get_db()

    today = datetime.now()
    curr_y = today.year
    curr_m = today.month
    current_ym_str = f"{curr_y}-{str(curr_m).zfill(2)}"
    # 获取所有未完成的任务
    tasks_to_check = conn.execute('SELECT id, date FROM tasks WHERE is_completed = 0').fetchall()
    migrated = False
    for t in tasks_to_check:
        d_str = t['date']
        if not d_str: continue

        task_ym_str = d_str[:7]
        # 如果任务所在月份小于当前月份，触发跨月迁跃
        if task_ym_str < current_ym_str:
            new_date = f"{current_ym_str}-99"
            conn.execute('UPDATE tasks SET date = ? WHERE id = ?', (new_date, t['id']))
            migrated = True

    if migrated:
        conn.commit()

    query = '''SELECT * FROM tasks WHERE SUBSTR(date, 1, 4) = ? '''
    tasks = [dict(t) for t in conn.execute(query, (year,)).fetchall()]
    conn.close()

    def custom_sort_key(task):
        # 1. 完成状态：完成的排在上面 (值为0)，未完成排下面 (值为1)
        is_completed_order = 0 if task.get('is_completed') == 1 else 1

        # 2. 是否为待定时间(-99)：具体日期排前面 (值为0)，待定的排最后 (值为1)
        date_str = task.get('date', '')
        is_tbd = 1 if date_str.endswith('-99') else 0

        # 3. 日期：按时间先后往下排
        parts = date_str.split('-')
        y = parts[0] if len(parts) > 0 else '9999'
        m = parts[1].zfill(2) if len(parts) > 1 else '99'
        d = parts[2].zfill(2) if len(parts) > 2 else '99'
        safe_date = f"{y}-{m}-{d}"

        sort_order = task.get('sort_order', 0)

        # 4. 创建时间：由于没有专门的创建时间字段，使用自增的 id 代替（id 越大代表创建越晚，在下面）
        task_id = task.get('id', 0)

        # 返回一个元组，Python 会依次按照元组里的 1、2、3、4 优先级进行排序
        return (is_completed_order, is_tbd, safe_date, sort_order, task_id)

    tasks.sort(key=custom_sort_key)
    return jsonify(tasks)


@app.route('/api/tasks/reorder', methods=['POST'])
def reorder_tasks():
    data = request.json
    ids = data.get('ids', [])
    conn = get_db()
    for index, task_id in enumerate(ids):
        conn.execute('UPDATE tasks SET sort_order = ? WHERE id = ?', (index, task_id))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@app.route('/api/calendar', methods=['GET'])
def get_calendar():
    year = int(request.args.get('year', datetime.now().year))
    cal_data = {}
    dt = datetime(year, 1, 1)

    custom_holidays = {
        "正月十五": "元宵节", "七月初七": "七夕节",
        "九月初九": "重阳节", "腊月初八": "腊八节",
        "腊月三十": "除夕", "腊月廿九": "除夕"
    }
    solar_holidays = {
        "02-14": "情人节", "03-08": "妇女节",
        "05-04": "青年节", "06-01": "儿童节",
        "09-10": "教师节", "12-25": "圣诞节"
    }
    official_holiday_map = {
        "New Year's Day": "元旦", "Spring Festival": "春节",
        "Tomb-sweeping Day": "清明节", "Labour Day": "劳动节",
        "Dragon Boat Festival": "端午节", "Mid-autumn Festival": "中秋节",
        "National Day": "国庆节"
    }

    # 一次性算出全年的农历，发给前端
    while dt.year == year:
        try:
            lunar_date = ZhDate.from_datetime(dt)
            lunar_str = lunar_date.chinese().split()[0][5:]

            is_holi, official_holiday_name = get_holiday_detail(dt)
            holiday_name = official_holiday_map.get(official_holiday_name, "")

            if not holiday_name:
                if lunar_str in custom_holidays:
                    holiday_name = custom_holidays[lunar_str]
                else:
                    date_mm_dd = dt.strftime("%m-%d")
                    if date_mm_dd in solar_holidays:
                        holiday_name = solar_holidays[date_mm_dd]

            if holiday_name:
                cal_data[dt.strftime('%Y-%m-%d')] = f"{lunar_str}({holiday_name})"
            else:
                cal_data[dt.strftime('%Y-%m-%d')] = lunar_str
        except Exception:
            pass
        dt += timedelta(days=1)

    return jsonify(cal_data)


@app.route('/api/tasks', methods=['POST'])
def add_task():
    data = request.json
    name = data.get('name', '')
    date_str = data.get('date')
    if date_str:
        parts = date_str.split('-')
        if len(parts) == 3:
            date_str = f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"

    priority = data.get('priority', 4)

    conn = get_db()

    row = conn.execute('SELECT MAX(sort_order) as max_sort FROM tasks').fetchone()
    max_sort = row['max_sort'] if row['max_sort'] is not None else 0
    new_sort_order = max_sort + 1
    # -----------------------------------------------------------------

    # 将 new_sort_order 顺带存入数据库
    cursor = conn.execute('INSERT INTO tasks (name, date, priority, sort_order) VALUES (?, ?, ?, ?)', (name, date_str, priority, new_sort_order))

    conn.commit()
    task_id = cursor.lastrowid
    conn.close()
    return jsonify({'id': task_id, 'status': 'success'})


@app.route('/api/tasks/<int:task_id>', methods=['PUT', 'DELETE'])
def update_task(task_id):
    conn = get_db()
    if request.method == 'DELETE':
        conn.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
    else:
        data = request.json
        updates = []
        params = []

        # 👇 核心优化：后端完全信任前端传来的标准化数据，不做任何二次解析
        for key in ['name', 'date', 'priority', 'is_completed']:
            if key in data:
                updates.append(f"{key} = ?")
                params.append(data[key])

        if updates:
            params.append(task_id)
            conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params)

    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@app.route('/api/tasks/import', methods=['POST'])
def import_tasks():
    if 'file' not in request.files: return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    wb = openpyxl.load_workbook(file)
    sheet = wb.active
    conn = get_db()
    for row in sheet.iter_rows(min_row=2):
        name_cell = row[1]
        date_cell = row[2]
        if not name_cell.value: continue

        is_completed = 1 if (name_cell.font and name_cell.font.strike) or (
                date_cell.font and date_cell.font.strike) else 0
        date_val = str(date_cell.value).split(' ')[
            0] if date_cell.value else f"{datetime.now().year}-{str(datetime.now().month).zfill(2)}-99"

        parts = date_val.split('-')
        if len(parts) == 3:
            date_val = f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"

        conn.execute('INSERT INTO tasks (name, date, priority, is_completed) VALUES (?, ?, ?, ?)',
                     (name_cell.value, date_val, 4, is_completed))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@app.route('/api/tasks/export', methods=['GET'])
def export_tasks():
    conn = get_db()
    # 导出所有任务，按id升序排序
    tasks = conn.execute('SELECT * FROM tasks ORDER BY id').fetchall()
    conn.close()

    wb = openpyxl.Workbook()
    sheet = wb.active
    sheet.title = "TodoList"
    # 定义表头
    sheet.append(['序号', '任务', '备注'])

    for idx, t in enumerate(tasks, 1):
        name = t['name']
        date_str = t['date']

        # 格式化日期：去除0。如果是空或待定(-99)则为空字符串
        remark = ""
        if date_str and not date_str.endswith('-99'):
            parts = date_str.split('-')
            if len(parts) >= 3:
                # int() 会自动去掉如 '05' 前面的 '0'
                remark = f"{int(parts[0])}-{int(parts[1])}-{int(parts[2])}"

        sheet.append([idx, name, remark])

    # 写入内存流供下载
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)

    # 按照需求生成文件名（精确到分钟）
    now_str = datetime.now().strftime('%Y%m%d%H%M')
    filename = f"Todo_List_{now_str}.xlsx"

    return send_file(
        out,
        as_attachment=True,
        download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


# ================= 远期目标接口 =================
@app.route('/api/goals', methods=['GET'])
def get_goals():
    conn = get_db()
    # 逻辑：获取所有未完成，以及最新完成的5个目标
    uncompleted = conn.execute(
        'SELECT * FROM long_term_goals WHERE is_completed = 0 ORDER BY created_at DESC').fetchall()
    completed = conn.execute(
        'SELECT * FROM long_term_goals WHERE is_completed = 1 ORDER BY created_at DESC LIMIT 3').fetchall()
    conn.close()

    # 合并后按照创建时间从新到旧排
    goals = [dict(g) for g in uncompleted + completed]
    goals.sort(key=lambda x: x['created_at'], reverse=True)
    return jsonify(goals)


@app.route('/api/goals', methods=['POST'])
def add_goal():
    conn = get_db()

    # 👇 新增：检查当前是否有远期目标，如果没有，则新目标默认为主要矛盾(1)
    row = conn.execute('SELECT COUNT(*) as count FROM long_term_goals').fetchone()
    is_first = 1 if row['count'] == 0 else 0

    # 修改：将 is_first 写入 is_principal_contradiction 列
    cursor = conn.execute('INSERT INTO long_term_goals (name, is_principal_contradiction) VALUES (?, ?)', ('新目标', is_first))
    conn.commit()
    goal_id = cursor.lastrowid
    conn.close()
    return jsonify({'id': goal_id, 'status': 'success'})


# 👇 新增：将指定目标设定为主要矛盾，其余目标自动取消主要矛盾
@app.route('/api/goals/<int:goal_id>/set_principal', methods=['POST'])
def set_principal_goal(goal_id):
    conn = get_db()
    # 1. 先把所有远期目标全部置 0
    conn.execute('UPDATE long_term_goals SET is_principal_contradiction = 0')
    # 2. 将当前指定的目标激活为主要矛盾 1
    conn.execute('UPDATE long_term_goals SET is_principal_contradiction = 1 WHERE id = ?', (goal_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


@app.route('/api/goals/<int:goal_id>', methods=['PUT', 'DELETE'])
def update_goal(goal_id):
    conn = get_db()
    if request.method == 'DELETE':
        conn.execute('DELETE FROM long_term_goals WHERE id = ?', (goal_id,))
    else:
        data = request.json
        updates = []
        params = []
        for key in ['name', 'is_completed']:
            if key in data:
                updates.append(f"{key} = ?")
                params.append(data[key])
        if updates:
            params.append(goal_id)
            conn.execute(f"UPDATE long_term_goals SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})


if __name__ == '__main__':
    app.run(port=16001, debug=True)