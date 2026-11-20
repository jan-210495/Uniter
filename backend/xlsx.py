"""Data-only OOXML workbook writer; no runtime spreadsheet dependencies.

All text uses inlineStr, never formulas, links, shared strings or macros. XML
is built by ElementTree (not string interpolation). Decimal numbers are written
as exact base-10 numeric cell values. Only the six required package parts are
emitted, with fixed internal paths and no external relationships.
"""
from datetime import timezone
from decimal import Decimal
from io import BytesIO
import re
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZIP_DEFLATED, ZipFile

MAX_DATA_ROWS = 1_048_575  # Excel's row limit minus the header
NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
# XML 1.0 disallows these characters, even in escaped text.
INVALID_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]')
HEADERS = {
    'en': ['Record type', 'Source', 'Reference', 'Timestamp (UTC)', 'Operator',
           'Amount (SYP)', 'Status', 'Sale name snapshot', 'Sale phone', 'Service', 'Performer', 'SIM'],
    'ar': ['نوع السجل', 'المصدر', 'المرجع', 'التاريخ والوقت (UTC)', 'المشغّل',
           'المبلغ (ل.س)', 'الحالة', 'اسم البيع المحفوظ', 'هاتف البيع', 'الخدمة', 'نفّذها', 'الشريحة'],
}
LABELS = {
    'en': {'sale': 'Sale', 'topup': 'Top-up', 'transaction': 'Sale transaction',
           'operator_topup': 'Operator credit top-up', 'sim_topup': 'SIM top-up', 'successful': 'Successful'},
    'ar': {'sale': 'بيع', 'topup': 'تعبئة رصيد', 'transaction': 'معاملة بيع',
           'operator_topup': 'تعبئة رصيد المشغّل', 'sim_topup': 'تعبئة شريحة', 'successful': 'ناجحة'},
}


def node(parent, tag, attrs=None, text=None):
    child = SubElement(parent, f'{{{NS}}}{tag}', attrs or {})
    child.text = text
    return child


def xml(root):
    return tostring(root, encoding='utf-8', xml_declaration=True)


def ledger_workbook(items, lang='en'):
    """One header row; dates are explicit UTC text, money is numeric SYP.

    Unavailable/not-applicable values are empty cells. No aggregate combines
    top-ups and sales. Literal inline strings neutralize dangerous prefixes,
    including those following whitespace, without modifying the stored value.
    """
    if len(items) > MAX_DATA_ROWS:
        raise ValueError('Excel worksheet row limit exceeded')
    labels = LABELS[lang]
    sheet = Element(f'{{{NS}}}worksheet')
    views = node(sheet, 'sheetViews')
    view = node(views, 'sheetView', {'workbookViewId': '0', 'rightToLeft': '1' if lang == 'ar' else '0'})
    node(view, 'pane', {'ySplit': '1', 'topLeftCell': 'A2', 'activePane': 'bottomLeft', 'state': 'frozen'})
    cols = node(sheet, 'cols')
    for i, width in enumerate([18, 26, 42, 24, 20, 20, 16, 28, 20, 26, 24, 30], 1):
        node(cols, 'col', {'min': str(i), 'max': str(i), 'width': str(width), 'customWidth': '1'})
    data = node(sheet, 'sheetData')

    def add_row(index, values):
        row = node(data, 'row', {'r': str(index)})
        for col, value in enumerate(values):
            attrs = {'r': f'{chr(65 + col)}{index}'}
            if value is None:
                node(row, 'c', attrs)
                continue
            if index == 1:
                attrs['s'] = '1'
            elif isinstance(value, Decimal):
                attrs['s'] = '2'
                cell = node(row, 'c', attrs)
                node(cell, 'v', text=format(value, 'f'))
                continue
            attrs['t'] = 'inlineStr'
            cell = node(row, 'c', attrs)
            inline = node(cell, 'is')
            # Excel's maximum cell text length; preserve Unicode including Arabic.
            text = INVALID_XML.sub('', str(value if value is not None else ''))[:32767]
            node(inline, 't', {'{http://www.w3.org/XML/1998/namespace}space': 'preserve'}, text)

    add_row(1, HEADERS[lang])
    for index, item in enumerate(items, 2):
        sale = item.record_type == 'sale'
        timestamp = item.created_at
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone.utc)
        add_row(index, [labels[item.record_type], labels[item.source], item.reference,
                        timestamp.strftime('%Y-%m-%d %H:%M:%S'), item.operator_name,
                        Decimal(item.amount_cents) / Decimal(100), labels[item.status],
                        item.customer_name if sale else None, item.customer_phone if sale else None,
                        item.service_name if sale else None, item.performed_by_username,
                        item.sim_display_name])
    node(sheet, 'autoFilter', {'ref': f'A1:L{len(items) + 1}'})

    workbook = Element(f'{{{NS}}}workbook')
    sheets = node(workbook, 'sheets')
    node(sheets, 'sheet', {'name': 'Transactions', 'sheetId': '1', f'{{{REL}}}id': 'rId1'})
    styles = Element(f'{{{NS}}}styleSheet')
    fonts = node(styles, 'fonts', {'count': '2'})
    for bold in (False, True):
        font = node(fonts, 'font')
        node(font, 'sz', {'val': '11'})
        node(font, 'name', {'val': 'Calibri'})
        if bold:
            node(font, 'b')
    fills = node(styles, 'fills', {'count': '2'})
    for pattern in ('none', 'gray125'):
        node(node(fills, 'fill'), 'patternFill', {'patternType': pattern})
    node(node(styles, 'borders', {'count': '1'}), 'border')
    for kind, count in [('cellStyleXfs', 1), ('cellXfs', 3)]:
        xfs = node(styles, kind, {'count': str(count)})
        for i in range(count):
            node(xfs, 'xf', {'numFmtId': '4' if i == 2 else '0', 'fontId': '1' if i == 1 else '0',
                             'fillId': '0', 'borderId': '0', 'xfId': '0'})
    node(node(styles, 'cellStyles', {'count': '1'}), 'cellStyle', {'name': 'Normal', 'xfId': '0', 'builtinId': '0'})

    types_ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
    types = Element('Types', xmlns=types_ns)
    for ext, content in [('rels', 'application/vnd.openxmlformats-package.relationships+xml'), ('xml', 'application/xml')]:
        SubElement(types, 'Default', Extension=ext, ContentType=content)
    for path, content in [('workbook.xml', 'sheet.main'), ('worksheets/sheet1.xml', 'worksheet'), ('styles.xml', 'styles')]:
        SubElement(types, 'Override', PartName='/xl/' + path,
                   ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.' + content + '+xml')

    def relationships(entries):
        root = Element('Relationships', xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
        for rid, kind, target in entries:
            SubElement(root, 'Relationship', Id=rid, Type=REL + '/' + kind, Target=target)
        return xml(root)

    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for path, content in {
            '[Content_Types].xml': xml(types),
            '_rels/.rels': relationships([('rId1', 'officeDocument', 'xl/workbook.xml')]),
            'xl/workbook.xml': xml(workbook),
            'xl/_rels/workbook.xml.rels': relationships([('rId1', 'worksheet', 'worksheets/sheet1.xml'), ('rId2', 'styles', 'styles.xml')]),
            'xl/styles.xml': xml(styles),
            'xl/worksheets/sheet1.xml': xml(sheet),
        }.items():
            archive.writestr(path, content)
    return output.getvalue()
