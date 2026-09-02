#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clean_tab — 表格数据清洗器 核心脚本（纯标准库，零依赖）

功能：
  读取一份 CSV / TSV 表格，执行一套可解释、可回滚的数据清洗流程：
    1. 表头标准化（去空格、合并空白、重复列名加后缀）
    2. 单元格清洗（去首尾空白、全角→半角、货币/千分位/百分比还原为数值）
    3. 字段类型推断（int / float / date / bool / string）
    4. 缺失值检测与统计（空串、占位符如 "-"/"NA"/"null"）
    5. 重复行检测（完全一致 + 关键列一致）
    6. 输出清洗后表格 + 结构化质检报告

用法：
  python clean_tab.py input.csv                      # 清洗并写回 input.cleaned.csv，同时打印报告
  python clean_tab.py input.csv --out out.csv        # 指定输出
  python clean_tab.py input.csv --report report.md   # 报告写文件（同时 stdout 摘要）
  python clean_tab.py input.csv --json               # 报告以 JSON 输出
  python clean_tab.py --selftest                     # 内置样例自检，exit 0 即通过

设计原则：
  - 仅依赖 csv / re / sys / os / json / datetime / argparse，可移植到任意环境。
  - 不修改任何源文件：输出均写到新文件，原始数据原样保留。
  - 所有清洗动作可追溯：报告逐条列出「列 / 动作 / 影响行数」。
"""

import csv
import re
import sys
import os
import json
import argparse
import datetime
from collections import OrderedDict, Counter

# ---------------------------------------------------------------------------
# 1. 字符与空白归一化
# ---------------------------------------------------------------------------
def fullwidth_to_halfwidth(s):
    """全角字符（含数字、字母、标点、空格）转半角。"""
    if not s:
        return s
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:               # 全角空格
            out.append(' ')
        elif code == 0xFFE5:             # 全角日元/人民币符号 ￥
            out.append('\u00a5')
        elif 0xFF01 <= code <= 0xFF5E:   # 全角 ! 到 ~
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return ''.join(out)


def normalize_cell(raw, coerce_numeric=True):
    """对单个单元格做通用清洗，返回 (clean_value, changed_flag, inferred_kind)。"""
    if raw is None:
        return '', True, 'empty'
    s = raw
    original = s
    s = fullwidth_to_halfwidth(s)
    s = s.strip()
    # 统一行内连续空白为一个空格（标签/名称类保留可读）
    s = re.sub(r'\s+', ' ', s)
    if s == '':
        return '', (original != ''), 'empty'

    changed = (s != original)

    # 缺失值占位符归一
    if s.lower() in ('-', 'na', 'n/a', 'null', 'none', 'nan', '无', '空', '暂无', '—', '--'):
        return '', True, 'empty'

    if coerce_numeric:
        # 货币 / 千分位 / 百分比 / 括号负数
        m = re.match(r'^[¥$€£]?\s*\(?-?[\d,]+(\.\d+)?\)?\s*%?$', s)
        if m:
            num = s.replace('¥', '').replace('$', '').replace('€', '').replace('£', '')
            num = num.replace(',', '').replace('%', '').replace('(', '-').replace(')', '')
            try:
                if '.' in num or re.search(r'\.\d', num):
                    val = float(num)
                else:
                    val = int(num)
                return str(val), True, 'number'
            except ValueError:
                pass
        # 日期：YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD
        if re.match(r'^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$', s):
            return s, changed, 'date'
        # 布尔
        if s.lower() in ('true', 'false', '是', '否', 'y', 'n', 'yes', 'no'):
            return s, changed, 'bool'

    return s, changed, 'string'


# ---------------------------------------------------------------------------
# 2. 表头标准化
# ---------------------------------------------------------------------------
def normalize_header(name, seen):
    s = fullwidth_to_halfwidth(name or '').strip()
    s = re.sub(r'\s+', '_', s)
    if s == '':
        s = 'col'
    if s in seen:
        i = 2
        while f"{s}_{i}" in seen:
            i += 1
        s = f"{s}_{i}"
    seen.add(s)
    return s


# ---------------------------------------------------------------------------
# 3. 类型推断（基于整列采样）
# ---------------------------------------------------------------------------
def infer_column_type(values):
    """根据非空样本推断列类型。"""
    non_empty = [v for v in values if v != '']
    if not non_empty:
        return 'string'
    num = sum(1 for v in non_empty if re.match(r'^-?\d+(\.\d+)?$', v))
    date = sum(1 for v in non_empty if re.match(r'^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$', v))
    if num >= len(non_empty) * 0.8:
        # 区分 int / float
        if all('.' not in v for v in non_empty):
            return 'int'
        return 'float'
    if date >= len(non_empty) * 0.8:
        return 'date'
    return 'string'


# ---------------------------------------------------------------------------
# 4. 主流程
# ---------------------------------------------------------------------------
PLACEHOLDERS = {'-', 'na', 'n/a', 'null', 'none', 'nan', '无', '空', '暂无', '—', '--'}

def clean_table(rows, delimiter=','):
    """对二维数据（list[list[str]]）执行清洗，返回 (headers, cleaned_rows, audit)。"""
    if not rows:
        return [], [], {'errors': ['空表']}
    header_raw = rows[0]
    data_rows = rows[1:]

    seen = set()
    headers = [normalize_header(h, seen) for h in header_raw]
    header_changed = any(h != fullwidth_to_halfwidth((header_raw[i] or '')).strip()
                         for i, h in enumerate(headers))

    cleaned = []
    cell_changes = 0
    per_col_types = []
    per_col_missing = [0] * len(headers)
    audit_log = []

    for r in data_rows:
        # 补齐/截断到表头长度
        r = (list(r) + [''] * len(headers))[:len(headers)]
        new_row = []
        for i, val in enumerate(r):
            clean_v, changed, kind = normalize_cell(val, coerce_numeric=True)
            if changed:
                cell_changes += 1
            if clean_v == '':
                per_col_missing[i] += 1
            new_row.append(clean_v)
        cleaned.append(new_row)

    # 列类型
    for i in range(len(headers)):
        col_vals = [row[i] for row in cleaned]
        per_col_types.append(infer_column_type(col_vals))

    if header_changed:
        audit_log.append({'action': '表头标准化', 'detail': '去空格/全角/重复列名加后缀',
                          'affected': len(headers)})

    # 缺失汇总
    total_cells = len(cleaned) * len(headers)
    missing_total = sum(per_col_missing)
    if missing_total:
        audit_log.append({'action': '缺失值检测', 'detail': f'共 {missing_total}/{total_cells} 个空/占位符单元格',
                          'affected': missing_total})

    # 重复行（完全一致）
    row_counter = Counter(tuple(r) for r in cleaned)
    exact_dup = sum(c - 1 for c in row_counter.values() if c > 1)
    if exact_dup:
        # 去重（保留首次出现）
        seen_rows = set()
        deduplicated = []
        for r in cleaned:
            key = tuple(r)
            if key not in seen_rows:
                seen_rows.add(key)
                deduplicated.append(r)
        removed = len(cleaned) - len(deduplicated)
        cleaned = deduplicated
        audit_log.append({'action': '整行去重', 'detail': '完全一致的重复行仅保留首条',
                          'affected': removed})

    # 关键列（首列）重复（内容重复但其他列可能不同，仅报告）
    if headers:
        first_col = [r[0] for r in cleaned]
        fc_counter = Counter(first_col)
        key_dup = sum(c - 1 for c in fc_counter.values() if c > 1 and r[0] != '')
        if key_dup:
            audit_log.append({'action': '主键列重复提示', 'detail': f'首列（{headers[0]}）存在 {key_dup} 个重复值，建议人工核对',
                              'affected': key_dup})

    audit = {
        'rows_in': len(data_rows),
        'rows_out': len(cleaned),
        'cells_changed': cell_changes,
        'missing_total': missing_total,
        'per_column': [
            {'name': headers[i], 'type': per_col_types[i], 'missing': per_col_missing[i],
             'missing_pct': round(per_col_missing[i] / max(len(cleaned), 1) * 100, 1)}
            for i in range(len(headers))
        ],
        'audit_log': audit_log,
        'headers': headers,
    }
    return headers, cleaned, audit


def read_table(path):
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=[',', '\t', ';', '|'])
            delim = dialect.delimiter
        except Exception:
            delim = ',' if sample.count(',') >= sample.count('\t') else '\t'
        reader = csv.reader(f, delimiter=delim)
        return [row for row in reader]


def write_csv(headers, rows, path):
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


def build_report(audit, fmt='md'):
    if fmt == 'json':
        return json.dumps(audit, ensure_ascii=False, indent=2)
    lines = []
    lines.append('# 数据清洗质检报告')
    lines.append('')
    lines.append(f"- 输入行数（不含表头）：{audit['rows_in']}")
    lines.append(f"- 输出行数：{audit['rows_out']}")
    lines.append(f"- 单元格清洗变更数：{audit['cells_changed']}")
    lines.append(f"- 缺失值总数（空/占位符）：{audit['missing_total']}")
    lines.append('')
    lines.append('## 字段概览')
    lines.append('')
    lines.append('| 字段 | 推断类型 | 缺失数 | 缺失率 |')
    lines.append('| --- | --- | --- | --- |')
    for c in audit['per_column']:
        lines.append(f"| {c['name']} | {c['type']} | {c['missing']} | {c['missing_pct']}% |")
    lines.append('')
    lines.append('## 清洗动作明细')
    lines.append('')
    if audit['audit_log']:
        for a in audit['audit_log']:
            lines.append(f"- **{a['action']}**：{a['detail']}（影响 {a['affected']}）")
    else:
        lines.append('- 未发现需修正项，数据基本干净。')
    lines.append('')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 5. 自检
# ---------------------------------------------------------------------------
def selftest():
    dirty = [
        ['姓名', ' 年龄 ', '工资(元)', '入职日期', '在职'],
        ['张三', ' 28 ', '￥12,000', '2023/01/15', '是'],
        ['李四', '３５', '15000.5', '2022-06-01', '否'],
        ['张三', ' 28 ', '￥12,000', '2023/01/15', '是'],   # 完全重复行
        ['王五', '', 'NULL', '2024.03.20', 'Y'],
        [' 赵六 ', '４２', ' 9,800 ', '2021/12/31', 'true'],
    ]
    headers, cleaned, audit = clean_table(dirty)
    # 断言
    assert '年龄' in headers, '表头去空格失败'
    assert audit['rows_out'] == 4, f"去重失败，期望4行，实际{audit['rows_out']}"
    # 找张三行，工资应被还原为 12000
    zhang = [r for r in cleaned if r[0].strip() == '张三'][0]
    assert zhang[2] == '12000', f"货币还原失败：{zhang[2]}"
    # 李四年龄全角 ３５ → 35
    li = [r for r in cleaned if '李四' in r[0]][0]
    assert li[1] == '35', f"全角转半角失败：{li[1]}"
    # 王五工资 NULL → 空（缺失）
    wang = [r for r in cleaned if '王五' in r[0]][0]
    assert wang[2] == '', f"占位符归一失败：{wang[2]}"
    # 列类型推断
    types = {c['name']: c['type'] for c in audit['per_column']}
    assert types.get('工资(元)') in ('int', 'float'), f"数值类型推断失败：{types}"
    assert types.get('入职日期') == 'date', f"日期类型推断失败：{types}"
    print('SELFTEST OK — 表头标准化 / 全角→半角 / 货币还原 / 去重 / 占位符归一 / 类型推断 全部通过')
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument('input', nargs='?', help='输入 CSV 路径')
    p.add_argument('--out', help='清洗后输出路径')
    p.add_argument('--report', help='报告输出路径')
    p.add_argument('--json', action='store_true', help='报告以 JSON 输出')
    p.add_argument('--selftest', action='store_true', help='内置样例自检')
    args = p.parse_args()

    if args.selftest:
        try:
            selftest()
            sys.exit(0)
        except AssertionError as e:
            print('SELFTEST FAIL:', e)
            sys.exit(1)

    if not args.input:
        print('用法：python clean_tab.py <input.csv> [--out out.csv] [--report report.md] [--json]')
        sys.exit(2)

    if not os.path.exists(args.input):
        print('文件不存在：', args.input)
        sys.exit(2)

    rows = read_table(args.input)
    headers, cleaned, audit = clean_table(rows)
    out_path = args.out or (os.path.splitext(args.input)[0] + '.cleaned.csv')
    write_csv(headers, cleaned, out_path)
    print(f'已写出清洗表：{out_path}（{audit["rows_out"]} 行，{len(headers)} 列）')
    report = build_report(audit, fmt='json' if args.json else 'md')
    if args.report:
        with open(args.report, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f'报告已写出：{args.report}')
    else:
        print('\n' + report)


if __name__ == '__main__':
    main()
