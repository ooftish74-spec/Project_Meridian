import logging
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, Spacer, PageBreak, Table, TableStyle, Image
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY

from src.report.pdf_generator import PDFReportGenerator

logger = logging.getLogger(__name__)

# Premium Colors inspired by McKinsey / Goldman Sachs
MCKINSEY_NAVY = colors.HexColor('#051C2C')
GS_BLUE = colors.HexColor('#7399C6')
QUANT_TEAL = colors.HexColor('#008080')
BACKGROUND_GRAY = colors.HexColor('#F8F9FA')

class UltimatePDFGenerator(PDFReportGenerator):
    """
    Premium PDF Generator with McKinsey / Goldman Sachs aesthetic.
    Inherits from the base PDFReportGenerator.
    """
    
    def setup_custom_styles(self):
        super().setup_custom_styles()
        
        # Override Base Styles for Premium Look
        
        # Premium Title
        self.styles.add(ParagraphStyle(
            name='UltimateTitle',
            parent=self.styles['CustomTitle'],
            fontSize=28,
            textColor=MCKINSEY_NAVY,
            spaceAfter=40,
            spaceBefore=60,
            alignment=TA_CENTER,
            fontName='Helvetica-Bold'
        ))
        
        # Premium Subtitle
        self.styles.add(ParagraphStyle(
            name='UltimateSubtitle',
            parent=self.styles['Normal'],
            fontSize=16,
            textColor=GS_BLUE,
            spaceAfter=60,
            alignment=TA_CENTER,
            fontName='Helvetica'
        ))
        
        # Title style (Premium)
        self.styles.add(ParagraphStyle(
            name='PremiumTitle',
            parent=self.styles['CustomTitle'],
            textColor=MCKINSEY_NAVY,
            fontSize=28,
            spaceAfter=25,
            alignment=1, # Center
            fontName='Helvetica-Bold'
        ))
        
        # Heading style (Chapter)
        self.styles.add(ParagraphStyle(
            name='PremiumHeading',
            parent=self.styles['CustomHeading'],
            textColor=MCKINSEY_NAVY,
            fontSize=18,
            spaceBefore=30,
            spaceAfter=12,
            fontName='Helvetica-Bold'
        ))
        
        # Override Subheading
        self.styles['CustomSubheading'].textColor = QUANT_TEAL
        self.styles['CustomSubheading'].fontSize = 14
        self.styles['CustomSubheading'].spaceBefore = 15
        self.styles['CustomSubheading'].spaceAfter = 8
        self.styles['CustomSubheading'].fontName = 'Helvetica-Bold'
        
        # Override Body
        self.styles['CustomBody'].fontSize = 11
        self.styles['CustomBody'].leading = 16
        self.styles['CustomBody'].textColor = colors.HexColor('#2E3B4E')
        
        # McKinsey Style Executive Summary Box
        self.styles.add(ParagraphStyle(
            name='ExecutiveSummary',
            parent=self.styles['Normal'],
            fontSize=12,
            textColor=colors.white,
            backColor=MCKINSEY_NAVY,
            borderPadding=(15, 20, 15, 20),
            spaceAfter=20,
            leading=18,
            fontName='Helvetica'
        ))

    def _create_premium_table(self, df):
        """Format a pandas DataFrame as a premium ReportLab Table."""
        data = [df.columns.values.tolist()] + df.values.tolist()
        
        table = Table(data)
        
        # Premium Table Style (Alternating Rows, Teal Header)
        style = TableStyle([
            # Header
            ('BACKGROUND', (0, 0), (-1, 0), MCKINSEY_NAVY),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 11),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('TOPPADDING', (0, 0), (-1, 0), 12),
            
            # Body
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 10),
            ('ALIGN', (1, 1), (-1, -1), 'RIGHT'),
            ('ALIGN', (0, 1), (0, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 8),
            ('TOPPADDING', (0, 1), (-1, -1), 8),
            ('TEXTCOLOR', (0, 1), (-1, -1), colors.HexColor('#333333')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, BACKGROUND_GRAY]),
        ])
        table.setStyle(style)
        return table

    def generate_report(self, filename: str, title: str, subtitle: str, sections: list) -> str:
        """Override generate_report to apply premium styling and title page."""
        output_path = self.output_dir / filename
        
        from reportlab.platypus import SimpleDocTemplate
        from reportlab.lib.pagesizes import letter
        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=letter,
            rightMargin=50, leftMargin=50,
            topMargin=50, bottomMargin=50
        )
        
        story = []
        
        # Cover Page
        story.append(Paragraph("MERIDIAN QUANTITATIVE RESEARCH", self.styles['UltimateSubtitle']))
        story.append(Paragraph(title, self.styles['UltimateTitle']))
        story.append(Paragraph(subtitle, self.styles['UltimateSubtitle']))
        story.append(Spacer(1, 100))
        story.append(Paragraph("STRICTLY CONFIDENTIAL", self.styles['UltimateSubtitle']))
        story.append(PageBreak())
        
        # Build sections
        for section in sections:
            sec_type = section.get('type')
            
            if sec_type == 'executive_summary':
                story.append(Paragraph(section.get('content', ''), self.styles['ExecutiveSummary']))
                story.append(Spacer(1, 15))
            elif sec_type == 'heading':
                story.append(Paragraph(section.get('title', ''), self.styles['PremiumHeading']))
                if 'content' in section:
                    story.append(Paragraph(section['content'], self.styles['CustomBody']))
                    story.append(Spacer(1, 10))
            elif sec_type == 'subheading':
                story.append(Paragraph(section.get('title', ''), self.styles['CustomSubheading']))
                if 'content' in section:
                    story.append(Paragraph(section['content'], self.styles['CustomBody']))
                    story.append(Spacer(1, 8))
            elif sec_type == 'text':
                story.append(Paragraph(section.get('content', ''), self.styles['CustomBody']))
                story.append(Spacer(1, 8))
            elif sec_type == 'bullets':
                if 'title' in section:
                    story.append(Paragraph(section['title'], self.styles['CustomSubheading']))
                for item in section.get('items', []):
                    # Bullet style
                    story.append(Paragraph(f"• {item}", self.styles['CustomBody']))
                story.append(Spacer(1, 10))
            elif sec_type == 'table':
                if 'title' in section:
                    story.append(Paragraph(section['title'], self.styles['CustomSubheading']))
                df = section.get('data')
                if df is not None and not df.empty:
                    table = self._create_premium_table(df)
                    story.append(table)
                story.append(Spacer(1, 15))
            elif sec_type == 'chart':
                if 'title' in section:
                    story.append(Paragraph(section['title'], self.styles['CustomSubheading']))
                chart_path = section.get('chart_path')
                if chart_path and Path(chart_path).exists():
                    img = Image(chart_path, width=450, height=300)
                    story.append(img)
                story.append(Spacer(1, 15))
                
        doc.build(story)
        return str(output_path)
