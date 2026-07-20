"""
PDF Report Generator
Generate professional economic reports in PDF format
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Image, KeepTogether
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

class PDFReportGenerator:
    """
    Generate PDF reports for economic analysis
    """

    def __init__(self, output_dir: str='reports'):
        """
        Initialize PDF report generator
        
        Args:
            output_dir: Directory to save reports
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f'PDFReportGenerator loaded from: {__file__}')
        logger.info(f'Table header textColor will be: colors.white')
        self.styles = getSampleStyleSheet()
        self.setup_custom_styles()

    def setup_custom_styles(self):
        """
        Setup custom paragraph styles
        """
        try:
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.pdfbase.pdfmetrics import registerFontFamily
            font_dir = _PROJECT_ROOT / 'assets' / 'fonts'
            reg_font_path = font_dir / 'NanumGothic-Regular.ttf'
            bold_font_path = font_dir / 'NanumGothic-Bold.ttf'
            if reg_font_path.exists():
                pdfmetrics.registerFont(TTFont('Helvetica', str(reg_font_path)))
                if bold_font_path.exists():
                    pdfmetrics.registerFont(TTFont('Helvetica-Bold', str(bold_font_path)))
            else:
                logger.error(f'NanumGothic fonts not found at {font_dir}')
        except Exception as e:
            logger.error(f'Could not load NanumGothic font: {e}')
        self.styles.add(ParagraphStyle(name='CustomTitle', parent=self.styles['Heading1'], fontSize=18, textColor=colors.HexColor('#0F2027'), spaceAfter=30, spaceBefore=20, alignment=TA_CENTER, fontName='Helvetica'))
        self.styles.add(ParagraphStyle(name='CustomHeading', parent=self.styles['Heading2'], fontSize=16, textColor=colors.HexColor('#0F2027'), spaceAfter=8, spaceBefore=24, fontName='Helvetica'))
        self.styles.add(ParagraphStyle(name='CustomSubheading', parent=self.styles['Heading3'], fontSize=14, textColor=colors.HexColor('#203A43'), spaceAfter=3, spaceBefore=18, fontName='Helvetica'))
        self.styles.add(ParagraphStyle(name='CustomBody', parent=self.styles['BodyText'], fontSize=10, leading=11, alignment=TA_JUSTIFY, spaceAfter=4, spaceBefore=0, fontName='Helvetica'))
        self.styles.add(ParagraphStyle(name='CustomBullet', parent=self.styles['BodyText'], fontSize=10, leading=11, leftIndent=20, bulletIndent=10, spaceAfter=3, spaceBefore=0))

    def create_cover_page(self, title: str, subtitle: str, date: str) -> List:
        """
        Create cover page elements
        """
        from reportlab.platypus import NextPageTemplate
        elements = []
        elements.append(Spacer(1, 4 * inch))
        cover_title_style = ParagraphStyle(name='CoverTitle', parent=self.styles['CustomTitle'], alignment=TA_LEFT, fontSize=32, leading=36, textColor=colors.HexColor('#0F2027'))
        title_para = Paragraph(title, cover_title_style)
        elements.append(title_para)
        elements.append(Spacer(1, 0.4 * inch))
        cover_sub_style = ParagraphStyle(name='CoverSubtitle', parent=self.styles['Heading2'], alignment=TA_LEFT, fontSize=16, textColor=colors.HexColor('#D4AF37'))
        subtitle_para = Paragraph(subtitle, cover_sub_style)
        elements.append(subtitle_para)
        elements.append(Spacer(1, 1 * inch))
        meta_style = ParagraphStyle(name='MetaStyle', parent=self.styles['Normal'], fontSize=11, alignment=TA_LEFT, textColor=colors.HexColor('#7F8C8D'))
        elements.append(Paragraph('PREPARED BY:', meta_style))
        elements.append(Paragraph('Meridian AI Quantitative Strategy Group', ParagraphStyle(name='MetaB', parent=meta_style, textColor=colors.black, fontName='Helvetica-Bold')))
        elements.append(Spacer(1, 0.3 * inch))
        elements.append(Paragraph('PUBLICATION DATE:', meta_style))
        elements.append(Paragraph(date, ParagraphStyle(name='MetaB2', parent=meta_style, textColor=colors.black, fontName='Helvetica-Bold')))
        elements.append(NextPageTemplate('Normal'))
        elements.append(PageBreak())
        return elements

    def create_table_of_contents(self, sections: List[str]) -> List:
        """
        Create table of contents
        
        Args:
            sections: List of section titles
        """
        elements = []
        toc_title = Paragraph('Table of Contents', self.styles['CustomHeading'])
        elements.append(toc_title)
        elements.append(Spacer(1, 0.2 * inch))
        for i, section in enumerate(sections, 1):
            toc_entry = Paragraph(f'{i}. {section}', self.styles['CustomBody'])
            elements.append(toc_entry)
        elements.append(PageBreak())
        return elements

    def create_section(self, title: str, content: str) -> List:
        """
        Create a section with title and content (keep together on same page)
        
        Args:
            title: Section title
            content: Section content
        """
        from reportlab.platypus import KeepTogether
        elements = []
        section_elements = []
        title_para = Paragraph(title, self.styles['CustomHeading'])
        section_elements.append(title_para)
        section_elements.append(Spacer(1, 0.05 * inch))
        formatted_content = content.replace('\n', '<br/>')
        content_para = Paragraph(formatted_content, self.styles['CustomBody'])
        section_elements.append(content_para)
        elements.append(KeepTogether(section_elements))
        elements.append(Spacer(1, 0.08 * inch))
        return elements

    def create_table(self, data: pd.DataFrame, title: Optional[str]=None) -> List:
        """
        Create a table from DataFrame with automatic column width adjustment and text wrapping
        
        Args:
            data: DataFrame to convert to table
            title: Optional table title
        """
        from reportlab.platypus import Paragraph, KeepTogether
        from reportlab.lib.styles import ParagraphStyle
        elements = []
        table_elements = []
        if title:
            title_para = Paragraph(title, self.styles['CustomSubheading'])
            table_elements.append(title_para)
            table_elements.append(Spacer(1, 0.005 * inch))
        header_style = ParagraphStyle('TableHeader', parent=None, fontSize=9, textColor=colors.white, alignment=1, fontName='Helvetica')
        cell_style = ParagraphStyle('TableCell', parent=self.styles['CustomBody'], fontSize=8, alignment=0, fontName='Helvetica', leading=10)
        table_data = []
        header_row = [Paragraph(str(col), header_style) for col in data.columns]
        table_data.append(header_row)
        for _, row in data.iterrows():
            data_row = [Paragraph(str(cell), cell_style) for cell in row]
            table_data.append(data_row)
        num_cols = len(data.columns)
        available_width = 6.5 * inch
        col_widths = []
        for col_idx in range(num_cols):
            max_len = len(str(data.columns[col_idx]))
            for row in data.values:
                max_len = max(max_len, len(str(row[col_idx])))
            width = min(max(0.7 * inch, max_len * 0.07 * inch), 3.0 * inch)
            col_widths.append(width)
        total_width = sum(col_widths)
        if total_width > available_width:
            col_widths = [w * available_width / total_width for w in col_widths]
        elif total_width < available_width:
            col_widths = [w * available_width / total_width for w in col_widths]
        table = Table(table_data, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F2027')), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white), ('ALIGN', (0, 0), (-1, 0), 'CENTER'), ('FONTNAME', (0, 0), (-1, 0), 'Helvetica'), ('FONTSIZE', (0, 0), (-1, 0), 9), ('BOTTOMPADDING', (0, 0), (-1, 0), 10), ('TOPPADDING', (0, 0), (-1, 0), 10), ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'), ('TEXTCOLOR', (0, 1), (-1, -1), colors.black), ('ALIGN', (0, 1), (-1, -1), 'LEFT'), ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'), ('FONTSIZE', (0, 1), (-1, -1), 8), ('TOPPADDING', (0, 1), (-1, -1), 6), ('BOTTOMPADDING', (0, 1), (-1, -1), 6), ('LEFTPADDING', (0, 1), (-1, -1), 6), ('RIGHTPADDING', (0, 1), (-1, -1), 6), ('VALIGN', (0, 1), (-1, -1), 'TOP'), ('GRID', (0, 0), (-1, -1), 0.5, colors.grey), ('BOX', (0, 0), (-1, -1), 1, colors.black), ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0f0f0')])]))
        table_elements.append(table)
        elements.append(KeepTogether(table_elements))
        elements.append(Spacer(1, 0.2 * inch))
        return elements

    def add_chart(self, chart_path: str, width: float=5.5 * inch, title: Optional[str]=None) -> List:
        """
        Add chart image to report with automatic size adjustment and KeepTogether
        
        Args:
            chart_path: Path to chart image
            width: Chart width (default: 5.5 inches to fit A4 page with margins)
            title: Optional chart title
        """
        from reportlab.platypus import KeepTogether
        elements = []
        chart_elements = []
        if title:
            title_para = Paragraph(title, self.styles['CustomSubheading'])
            chart_elements.append(title_para)
            chart_elements.append(Spacer(1, 0.05 * inch))
        try:
            img = Image(chart_path)
            aspect = img.imageHeight / img.imageWidth
            img.drawWidth = width
            img.drawHeight = width * aspect
            max_width = 6.5 * inch
            max_height = 4.0 * inch
            if img.drawWidth > max_width:
                img.drawWidth = max_width
                img.drawHeight = max_width * aspect
            if img.drawHeight > max_height:
                img.drawHeight = max_height
                img.drawWidth = max_height / aspect
            chart_elements.append(img)
        except Exception as e:
            logger.error(f'Error adding chart {chart_path}: {e}')
            error_para = Paragraph(f'[Chart not available: {chart_path}]', self.styles['CustomBody'])
            chart_elements.append(error_para)
        if chart_elements:
            elements.append(KeepTogether(chart_elements))
            elements.append(Spacer(1, 0.15 * inch))
        return elements

    def create_bullet_list(self, items: List[str], title: Optional[str]=None) -> List:
        """
        Create bullet list with compact spacing and keep together on same page
        
        Args:
            items: List of items
            title: Optional list title
        """
        from reportlab.platypus import Paragraph, KeepTogether
        from reportlab.lib.styles import ParagraphStyle
        elements = []
        section_elements = []
        if title:
            title_para = Paragraph(title, self.styles['CustomSubheading'])
            section_elements.append(title_para)
            section_elements.append(Spacer(1, 0.06 * inch))
        bullet_style = ParagraphStyle('BulletItem', parent=self.styles['CustomBody'], fontSize=9.5, leading=13, leftIndent=15, rightIndent=10, spaceBefore=6, spaceAfter=6, firstLineIndent=0, bulletIndent=5)
        for i, item in enumerate(items):
            bullet_para = Paragraph(f'• {item}', bullet_style)
            section_elements.append(bullet_para)
            if i < len(items) - 1:
                section_elements.append(Spacer(1, 0.06 * inch))
        elements.append(KeepTogether(section_elements))
        elements.append(Spacer(1, 0.08 * inch))
        return elements

    def _draw_cover_background(self, canvas, doc):
        """Draws the JP Morgan style cover page background"""
        canvas.saveState()
        canvas.setFillColor(colors.HexColor('#0F2027'))
        canvas.rect(0, 0, 180, A4[1], fill=1, stroke=0)
        canvas.setFillColor(colors.HexColor('#D4AF37'))
        canvas.rect(180, 0, 8, A4[1], fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont('Helvetica-Bold', 40)
        canvas.translate(90, 150)
        canvas.rotate(90)
        canvas.drawString(0, 0, 'MERIDIAN CAPITAL')
        canvas.restoreState()

    def _draw_header_footer(self, canvas, doc):
        """Draws the standard header and footer for normal pages"""
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor('#0F2027'))
        canvas.setLineWidth(1.5)
        canvas.line(doc.leftMargin, A4[1] - 40, A4[0] - doc.rightMargin, A4[1] - 40)
        canvas.setFont('Helvetica-Bold', 9)
        canvas.setFillColor(colors.HexColor('#203A43'))
        canvas.drawString(doc.leftMargin, A4[1] - 32, 'MERIDIAN INVESTMENT STRATEGY')
        canvas.setFont('Helvetica', 9)
        canvas.setFillColor(colors.HexColor('#7F8C8D'))
        canvas.drawRightString(A4[0] - doc.rightMargin, A4[1] - 32, datetime.now().strftime('%Y-%m-%d'))
        canvas.setStrokeColor(colors.HexColor('#D4AF37'))
        canvas.setLineWidth(1)
        canvas.line(doc.leftMargin, 50, A4[0] - doc.rightMargin, 50)
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#A0A0A0'))
        canvas.drawString(doc.leftMargin, 35, 'Meridian Capital Group Confidential & Proprietary')
        canvas.drawRightString(A4[0] - doc.rightMargin, 35, f'Page {doc.page}')
        canvas.restoreState()

    def generate_report(self, filename: str, title: str, subtitle: str, sections: List[Dict]) -> str:
        """
        Generate complete PDF report
        
        Args:
            filename: Output filename
            title: Report title
            subtitle: Report subtitle
            sections: List of section dictionaries with 'title', 'content', 'type', etc.
        
        Returns:
            Path to generated PDF
        """
        logger.info('=' * 80)
        logger.info(f'Generating PDF Report: {title}')
        logger.info('=' * 80)
        output_path = self.output_dir / filename
        from reportlab.platypus import BaseDocTemplate, PageTemplate, Frame
        doc = BaseDocTemplate(str(output_path), pagesize=A4, rightMargin=50, leftMargin=50, topMargin=70, bottomMargin=60)
        frame_cover = Frame(200, doc.bottomMargin, A4[0] - 250, A4[1] - 130, id='cover_frame')
        frame_normal = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id='normal_frame')
        template_cover = PageTemplate(id='Cover', frames=frame_cover, onPage=self._draw_cover_background)
        template_normal = PageTemplate(id='Normal', frames=frame_normal, onPage=self._draw_header_footer)
        doc.addPageTemplates([template_cover, template_normal])
        story = []
        story.extend(self.create_cover_page(title=title, subtitle=subtitle, date=datetime.now().strftime('%Y-%m-%d')))
        section_titles = [s.get('title', '') for s in sections if s.get('type') == 'heading' and s.get('title', '')]
        story.extend(self.create_table_of_contents(section_titles))
        i = 0
        while i < len(sections):
            section = sections[i]
            section_type = section.get('type', 'text')
            if section_type == 'text':
                from reportlab.platypus import KeepTogether
                if i + 1 < len(sections) and sections[i + 1].get('type') == 'subheading':
                    section_elements = []
                    title = section.get('title', '')
                    content = section.get('content', '')
                    if title:
                        title_para = Paragraph(title, self.styles['CustomTitle'])
                        section_elements.append(title_para)
                        section_elements.append(Spacer(1, 0.08 * inch))
                    if content:
                        content_para = Paragraph(content, self.styles['CustomBody'])
                        section_elements.append(content_para)
                        section_elements.append(Spacer(1, 0.05 * inch))
                    next_idx = i + 1
                    subheading_section = sections[next_idx]
                    sub_title = subheading_section.get('title', '')
                    sub_content = subheading_section.get('content', '')
                    if sub_title:
                        sub_title_para = Paragraph(sub_title, self.styles['CustomSubheading'])
                        section_elements.append(sub_title_para)
                    if sub_content:
                        section_elements.append(Spacer(1, 0.03 * inch))
                        sub_content_para = Paragraph(sub_content, self.styles['CustomBody'])
                        section_elements.append(sub_content_para)
                    sections_to_skip = 1
                    next_idx += 1
                    while next_idx < len(sections) and sections[next_idx].get('type') == 'table':
                        section_elements.append(Spacer(1, 0.05 * inch))
                        table_section = sections[next_idx]
                        table_data = table_section.get('data')
                        table_title = table_section.get('title')
                        if table_title:
                            table_title_para = Paragraph(table_title, self.styles['CustomSubheading'])
                            section_elements.append(table_title_para)
                            section_elements.append(Spacer(1, 0.005 * inch))
                        if table_data is not None:
                            from reportlab.lib.styles import ParagraphStyle
                            header_style = ParagraphStyle('TableHeader', parent=None, fontSize=9, textColor=colors.white, fontName='Helvetica', alignment=TA_CENTER)
                            if hasattr(table_data, 'values'):
                                headers = list(table_data.columns)
                                data_rows = table_data.values.tolist()
                            else:
                                headers = list(table_data[0].keys())
                                data_rows = [[row[key] for key in headers] for row in table_data]
                            wrapped_headers = [Paragraph(str(h), header_style) for h in headers]
                            wrapped_data = []
                            for row in data_rows:
                                wrapped_row = [Paragraph(str(cell), self.styles['CustomBody']) for cell in row]
                                wrapped_data.append(wrapped_row)
                            table_data_final = [wrapped_headers] + wrapped_data
                            col_widths = [6.5 * inch / len(headers)] * len(headers)
                            from reportlab.platypus import Table, TableStyle
                            table = Table(table_data_final, colWidths=col_widths, repeatRows=1)
                            table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F2027')), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white), ('ALIGN', (0, 0), (-1, 0), 'CENTER'), ('FONTNAME', (0, 0), (-1, 0), 'Helvetica'), ('FONTSIZE', (0, 0), (-1, 0), 9), ('BOTTOMPADDING', (0, 0), (-1, 0), 10), ('TOPPADDING', (0, 0), (-1, 0), 10), ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'), ('TEXTCOLOR', (0, 1), (-1, -1), colors.black), ('ALIGN', (0, 1), (-1, -1), 'LEFT'), ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'), ('FONTSIZE', (0, 1), (-1, -1), 9), ('TOPPADDING', (0, 1), (-1, -1), 6), ('BOTTOMPADDING', (0, 1), (-1, -1), 6), ('GRID', (0, 0), (-1, -1), 0.5, colors.grey), ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0f0f0')])]))
                            section_elements.append(table)
                        sections_to_skip += 1
                        next_idx += 1
                    if next_idx < len(sections) and sections[next_idx].get('type') == 'bullets':
                        bullets_section = sections[next_idx]
                        bullets_title = bullets_section.get('title')
                        bullets_items = bullets_section.get('items', [])
                        section_elements.append(Spacer(1, 0.05 * inch))
                        if bullets_title:
                            bullets_title_para = Paragraph(bullets_title, self.styles['CustomSubheading'])
                            section_elements.append(bullets_title_para)
                            section_elements.append(Spacer(1, 0.03 * inch))
                        for idx, item in enumerate(bullets_items):
                            if item and item.strip():
                                bullet_para = Paragraph(f'• {item}', self.styles['CustomBody'])
                                section_elements.append(bullet_para)
                                if idx < len(bullets_items) - 1:
                                    section_elements.append(Spacer(1, 0.06 * inch))
                        sections_to_skip += 1
                    story.append(KeepTogether(section_elements))
                    story.append(Spacer(1, 0.1 * inch))
                    i += sections_to_skip + 1
                    continue
                elif i + 1 < len(sections) and sections[i + 1].get('type') == 'table':
                    section_elements = []
                    title = section.get('title', '')
                    content = section.get('content', '')
                    if title:
                        title_para = Paragraph(title, self.styles['CustomTitle'])
                        section_elements.append(title_para)
                        section_elements.append(Spacer(1, 0.08 * inch))
                    if content:
                        content_para = Paragraph(content, self.styles['CustomBody'])
                        section_elements.append(content_para)
                        section_elements.append(Spacer(1, 0.05 * inch))
                    next_idx = i + 1
                    sections_to_skip = 0
                    while next_idx < len(sections) and sections[next_idx].get('type') == 'table':
                        section_elements.append(Spacer(1, 0.05 * inch))
                        table_section = sections[next_idx]
                        table_data = table_section.get('data')
                        table_title = table_section.get('title')
                        if table_title:
                            table_title_para = Paragraph(table_title, self.styles['CustomSubheading'])
                            section_elements.append(table_title_para)
                            section_elements.append(Spacer(1, 0.005 * inch))
                        if table_data is not None:
                            from reportlab.lib.styles import ParagraphStyle
                            header_style = ParagraphStyle('TableHeader', parent=None, fontSize=9, textColor=colors.white, fontName='Helvetica', alignment=TA_CENTER)
                            if hasattr(table_data, 'values'):
                                headers = list(table_data.columns)
                                data_rows = table_data.values.tolist()
                            else:
                                headers = list(table_data[0].keys())
                                data_rows = [[row[key] for key in headers] for row in table_data]
                            wrapped_headers = [Paragraph(str(h), header_style) for h in headers]
                            wrapped_data = []
                            for row in data_rows:
                                wrapped_row = [Paragraph(str(cell), self.styles['CustomBody']) for cell in row]
                                wrapped_data.append(wrapped_row)
                            table_data_final = [wrapped_headers] + wrapped_data
                            col_widths = [6.5 * inch / len(headers)] * len(headers)
                            from reportlab.platypus import Table, TableStyle
                            table = Table(table_data_final, colWidths=col_widths, repeatRows=1)
                            table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F2027')), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white), ('ALIGN', (0, 0), (-1, 0), 'CENTER'), ('FONTNAME', (0, 0), (-1, 0), 'Helvetica'), ('FONTSIZE', (0, 0), (-1, 0), 9), ('BOTTOMPADDING', (0, 0), (-1, 0), 10), ('TOPPADDING', (0, 0), (-1, 0), 10), ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'), ('TEXTCOLOR', (0, 1), (-1, -1), colors.black), ('ALIGN', (0, 1), (-1, -1), 'LEFT'), ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'), ('FONTSIZE', (0, 1), (-1, -1), 9), ('TOPPADDING', (0, 1), (-1, -1), 6), ('BOTTOMPADDING', (0, 1), (-1, -1), 6), ('GRID', (0, 0), (-1, -1), 0.5, colors.grey), ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0f0f0')])]))
                            section_elements.append(table)
                        sections_to_skip += 1
                        next_idx += 1
                    if next_idx < len(sections) and sections[next_idx].get('type') == 'bullets':
                        bullets_section = sections[next_idx]
                        bullets_title = bullets_section.get('title')
                        bullets_items = bullets_section.get('items', [])
                        section_elements.append(Spacer(1, 0.05 * inch))
                        if bullets_title:
                            bullets_title_para = Paragraph(bullets_title, self.styles['CustomSubheading'])
                            section_elements.append(bullets_title_para)
                            section_elements.append(Spacer(1, 0.03 * inch))
                        for idx, item in enumerate(bullets_items):
                            if item and item.strip():
                                bullet_para = Paragraph(f'• {item}', self.styles['CustomBody'])
                                section_elements.append(bullet_para)
                                if idx < len(bullets_items) - 1:
                                    section_elements.append(Spacer(1, 0.06 * inch))
                        sections_to_skip += 1
                    story.append(KeepTogether(section_elements))
                    story.append(Spacer(1, 0.1 * inch))
                    i += sections_to_skip + 1
                    continue
                else:
                    story.extend(self.create_section(title=section.get('title', ''), content=section.get('content', '')))
            elif section_type == 'heading':
                from reportlab.platypus import KeepTogether
                section_elements = []
                sections_to_skip = 0
                title = section.get('title', '')
                content = section.get('content', '')
                if title:
                    heading_para = Paragraph(title, self.styles['CustomHeading'])
                    section_elements.append(heading_para)
                if content:
                    content_para = Paragraph(content, self.styles['CustomBody'])
                    section_elements.append(content_para)
                    section_elements.append(Spacer(1, 0.05 * inch))
                if i + 1 < len(sections):
                    next_section = sections[i + 1]
                    next_type = next_section.get('type')
                    if next_type == 'subheading':
                        sub_title = next_section.get('title', '')
                        sub_content = next_section.get('content', '')
                        if sub_title:
                            sub_title_para = Paragraph(sub_title, self.styles['CustomSubheading'])
                            section_elements.append(sub_title_para)
                        if sub_content:
                            section_elements.append(Spacer(1, 0.03 * inch))
                            sub_content_para = Paragraph(sub_content, self.styles['CustomBody'])
                            section_elements.append(sub_content_para)
                        sections_to_skip = 1
                        if i + 2 < len(sections) and sections[i + 2].get('type') == 'table':
                            table_section = sections[i + 2]
                            table_data = table_section.get('data')
                            table_title = table_section.get('title')
                            if table_data is not None:
                                section_elements.append(Spacer(1, 0.05 * inch))
                                from reportlab.lib.styles import ParagraphStyle
                                header_style = ParagraphStyle('TableHeader', parent=None, fontSize=9, textColor=colors.white, fontName='Helvetica', alignment=TA_CENTER)
                                if hasattr(table_data, 'values'):
                                    headers = list(table_data.columns)
                                    data_rows = table_data.values.tolist()
                                else:
                                    headers = list(table_data[0].keys())
                                    data_rows = [[row[key] for key in headers] for row in table_data]
                                wrapped_headers = [Paragraph(str(h), header_style) for h in headers]
                                wrapped_data = []
                                for row in data_rows:
                                    wrapped_row = [Paragraph(str(cell), self.styles['CustomBody']) for cell in row]
                                    wrapped_data.append(wrapped_row)
                                table_data_final = [wrapped_headers] + wrapped_data
                                col_widths = [6.5 * inch / len(headers)] * len(headers)
                                from reportlab.platypus import Table, TableStyle
                                table = Table(table_data_final, colWidths=col_widths, repeatRows=1)
                                table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F2027')), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white), ('ALIGN', (0, 0), (-1, 0), 'CENTER'), ('FONTNAME', (0, 0), (-1, 0), 'Helvetica'), ('FONTSIZE', (0, 0), (-1, 0), 9), ('BOTTOMPADDING', (0, 0), (-1, 0), 10), ('TOPPADDING', (0, 0), (-1, 0), 10), ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'), ('TEXTCOLOR', (0, 1), (-1, -1), colors.black), ('ALIGN', (0, 1), (-1, -1), 'LEFT'), ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'), ('FONTSIZE', (0, 1), (-1, -1), 9), ('TOPPADDING', (0, 1), (-1, -1), 6), ('BOTTOMPADDING', (0, 1), (-1, -1), 6), ('GRID', (0, 0), (-1, -1), 0.5, colors.grey), ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0f0f0')])]))
                                section_elements.append(table)
                                sections_to_skip = 2
                story.append(KeepTogether(section_elements))
                i += sections_to_skip + 1
                continue
            elif section_type == 'table':
                story.extend(self.create_table(data=section.get('data'), title=section.get('title')))
            elif section_type == 'chart':
                story.extend(self.add_chart(chart_path=section.get('chart_path'), title=section.get('title')))
            elif section_type == 'subheading':
                from reportlab.platypus import KeepTogether
                subheading_elements = []
                title_para = Paragraph(section.get('title', ''), self.styles['CustomSubheading'])
                subheading_elements.append(title_para)
                if section.get('content'):
                    subheading_elements.append(Spacer(1, 0.03 * inch))
                    content_para = Paragraph(section.get('content', ''), self.styles['CustomBody'])
                    subheading_elements.append(content_para)
                next_idx = i + 1
                sections_to_skip = 0
                while next_idx < len(sections) and sections[next_idx].get('type') == 'table':
                    subheading_elements.append(Spacer(1, 0.05 * inch))
                    table_section = sections[next_idx]
                    table_data = table_section.get('data')
                    table_title = table_section.get('title')
                    if table_title:
                        table_title_para = Paragraph(table_title, self.styles['CustomSubheading'])
                        subheading_elements.append(table_title_para)
                        subheading_elements.append(Spacer(1, 0.005 * inch))
                    if table_data is not None:
                        from reportlab.lib.styles import ParagraphStyle
                        header_style = ParagraphStyle('TableHeader', parent=None, fontSize=9, textColor=colors.white, fontName='Helvetica', alignment=TA_CENTER)
                        if hasattr(table_data, 'values'):
                            headers = list(table_data.columns)
                            data_rows = table_data.values.tolist()
                        else:
                            headers = list(table_data[0].keys())
                            data_rows = [[row[key] for key in headers] for row in table_data]
                        wrapped_headers = [Paragraph(str(h), header_style) for h in headers]
                        wrapped_data = []
                        for row in data_rows:
                            wrapped_row = [Paragraph(str(cell), self.styles['CustomBody']) for cell in row]
                            wrapped_data.append(wrapped_row)
                        table_data_final = [wrapped_headers] + wrapped_data
                        col_widths = [6.5 * inch / len(headers)] * len(headers)
                        from reportlab.platypus import Table, TableStyle
                        table = Table(table_data_final, colWidths=col_widths, repeatRows=1)
                        table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F2027')), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white), ('ALIGN', (0, 0), (-1, 0), 'CENTER'), ('FONTNAME', (0, 0), (-1, 0), 'Helvetica'), ('FONTSIZE', (0, 0), (-1, 0), 9), ('BOTTOMPADDING', (0, 0), (-1, 0), 10), ('TOPPADDING', (0, 0), (-1, 0), 10), ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'), ('TEXTCOLOR', (0, 1), (-1, -1), colors.black), ('ALIGN', (0, 1), (-1, -1), 'LEFT'), ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'), ('FONTSIZE', (0, 1), (-1, -1), 9), ('TOPPADDING', (0, 1), (-1, -1), 6), ('BOTTOMPADDING', (0, 1), (-1, -1), 6), ('GRID', (0, 0), (-1, -1), 0.5, colors.grey), ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0f0f0')])]))
                        subheading_elements.append(table)
                    sections_to_skip += 1
                    next_idx += 1
                if next_idx < len(sections) and sections[next_idx].get('type') == 'bullets':
                    bullets_section = sections[next_idx]
                    bullets_title = bullets_section.get('title')
                    bullets_items = bullets_section.get('items', [])
                    subheading_elements.append(Spacer(1, 0.05 * inch))
                    if bullets_title:
                        bullets_title_para = Paragraph(bullets_title, self.styles['CustomSubheading'])
                        subheading_elements.append(bullets_title_para)
                        subheading_elements.append(Spacer(1, 0.03 * inch))
                    for idx, item in enumerate(bullets_items):
                        if item and item.strip():
                            bullet_para = Paragraph(f'• {item}', self.styles['CustomBody'])
                            subheading_elements.append(bullet_para)
                            if idx < len(bullets_items) - 1:
                                subheading_elements.append(Spacer(1, 0.06 * inch))
                    sections_to_skip += 1
                if sections_to_skip > 0:
                    story.append(KeepTogether(subheading_elements))
                    story.append(Spacer(1, 0.1 * inch))
                    i += sections_to_skip + 1
                    continue
                else:
                    story.append(KeepTogether(subheading_elements))
                    story.append(Spacer(1, 0.08 * inch))
            elif section_type == 'bullets':
                story.extend(self.create_bullet_list(items=section.get('items', []), title=section.get('title')))
            elif section_type == 'executive_box':
                from reportlab.platypus import KeepTogether
                box_elements = []
                content_items = section.get('content', [])
                for item in content_items:
                    if item and item.strip():
                        box_para = Paragraph(item, self.styles['CustomBody'])
                        box_elements.append(box_para)
                        box_elements.append(Spacer(1, 0.08 * inch))
                if box_elements:
                    story.append(KeepTogether(box_elements))
                    story.append(Spacer(1, 0.15 * inch))
            elif section_type == 'subsection':
                from reportlab.platypus import KeepTogether
                subsection_elements = []
                title = section.get('title', '')
                content = section.get('content')
                if title:
                    title_para = Paragraph(f'<b>{title}</b>', self.styles['CustomBody'])
                    subsection_elements.append(title_para)
                    subsection_elements.append(Spacer(1, 0.05 * inch))
                if content:
                    if isinstance(content, list):
                        for item in content:
                            if item and item.strip():
                                bullet_para = Paragraph(f'• {item}', self.styles['CustomBody'])
                                subsection_elements.append(bullet_para)
                                subsection_elements.append(Spacer(1, 0.04 * inch))
                    elif isinstance(content, dict):
                        for key, value in content.items():
                            kv_para = Paragraph(f'<b>{key}:</b> {value}', self.styles['CustomBody'])
                            subsection_elements.append(kv_para)
                            subsection_elements.append(Spacer(1, 0.04 * inch))
                    else:
                        content_para = Paragraph(str(content), self.styles['CustomBody'])
                        subsection_elements.append(content_para)
                if subsection_elements:
                    story.append(KeepTogether(subsection_elements))
                    story.append(Spacer(1, 0.1 * inch))
            elif section_type == 'scenario_box':
                from reportlab.platypus import KeepTogether
                scenario_elements = []
                scenario_data = section.get('scenario', {})
                name = scenario_data.get('name', '')
                title = scenario_data.get('title', '')
                probability = scenario_data.get('probability', 0)
                if name and title:
                    header = f'{name}: {title} ({probability}% probability)'
                    header_para = Paragraph(f'<b>{header}</b>', self.styles['CustomSubheading'])
                    scenario_elements.append(header_para)
                    scenario_elements.append(Spacer(1, 0.08 * inch))
                for key in ['gdp_growth', 'inflation', 'fed_policy', 'sp500_return', 'kospi_return']:
                    if key in scenario_data:
                        label = key.replace('_', ' ').title()
                        value = scenario_data[key]
                        metric_para = Paragraph(f'<b>{label}:</b> {value}', self.styles['CustomBody'])
                        scenario_elements.append(metric_para)
                        scenario_elements.append(Spacer(1, 0.03 * inch))
                if 'drivers' in scenario_data:
                    scenario_elements.append(Spacer(1, 0.05 * inch))
                    drivers_para = Paragraph('<b>Key Drivers:</b>', self.styles['CustomBody'])
                    scenario_elements.append(drivers_para)
                    scenario_elements.append(Spacer(1, 0.03 * inch))
                    for driver in scenario_data['drivers']:
                        driver_para = Paragraph(f'• {driver}', self.styles['CustomBody'])
                        scenario_elements.append(driver_para)
                        scenario_elements.append(Spacer(1, 0.03 * inch))
                if scenario_elements:
                    story.append(KeepTogether(scenario_elements))
                    story.append(Spacer(1, 0.15 * inch))
            elif section_type == 'scenario_summary':
                summary_data = section.get('data', {})
                summary_para = Paragraph(f'<b>Base Case Probability:</b> {summary_data.get('base_probability', 0)}% | <b>Expected Return:</b> {summary_data.get('expected_return', 'N/A')} | <b>Recommendation:</b> {summary_data.get('recommendation', 'N/A')}', self.styles['CustomBody'])
                story.append(summary_para)
                story.append(Spacer(1, 0.1 * inch))
            elif section_type == 'decision_tree':
                tree_data = section.get('data', {})
                rec_para = Paragraph(f'<b>Primary Recommendation:</b> {tree_data.get('primary_recommendation', 'N/A')}', self.styles['CustomSubheading'])
                story.append(rec_para)
                story.append(Spacer(1, 0.08 * inch))
                if 'monitoring_indicators' in tree_data:
                    indicators_para = Paragraph('<b>Key Monitoring Indicators:</b>', self.styles['CustomBody'])
                    story.append(indicators_para)
                    story.append(Spacer(1, 0.05 * inch))
                    for indicator in tree_data['monitoring_indicators']:
                        ind_para = Paragraph(f'• {indicator}', self.styles['CustomBody'])
                        story.append(ind_para)
                        story.append(Spacer(1, 0.03 * inch))
                story.append(Spacer(1, 0.1 * inch))
            elif section_type == 'divider':
                style = section.get('style', 'medium')
                if style == 'thick':
                    story.append(Spacer(1, 0.15 * inch))
                elif style == 'medium':
                    story.append(Spacer(1, 0.1 * inch))
                else:
                    story.append(Spacer(1, 0.05 * inch))
            elif section_type == 'subtitle':
                text = section.get('text', '')
                if text:
                    subtitle_para = Paragraph(text, self.styles['CustomBody'])
                    story.append(subtitle_para)
                    story.append(Spacer(1, 0.1 * inch))
            elif section_type == 'pagebreak':
                story.append(PageBreak())
            i += 1
        try:
            doc.build(story)
            logger.info(f'✓ PDF report generated: {output_path}')
            return str(output_path)
        except Exception as e:
            logger.error(f'Error generating PDF: {e}')
            return ''