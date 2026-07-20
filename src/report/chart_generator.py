"""
Chart Generation Module
Economic indicators, stock charts, correlation heatmaps
"""
import logging
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import seaborn as sns
import plotly.graph_objects as go
import plotly.express as px
from pathlib import Path
from typing import Dict, List, Optional, Tuple
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
logger = logging.getLogger(__name__)
plt.style.use('seaborn-v0_8-whitegrid')
plt.rcParams['axes.facecolor'] = '#F8F9FA'
plt.rcParams['figure.facecolor'] = 'white'
plt.rcParams['grid.color'] = '#EAEAEA'
plt.rcParams['grid.linestyle'] = '--'
plt.rcParams['axes.edgecolor'] = '#CCCCCC'
plt.rcParams['axes.linewidth'] = 1.0
plt.rcParams['axes.spines.top'] = False
plt.rcParams['axes.spines.right'] = False
plt.rcParams['legend.frameon'] = False
plt.rcParams['legend.loc'] = 'upper left'
meridian_colors = ['#0F2027', '#D4AF37', '#4B79A1', '#78a2cc', '#A0A0A0', '#203A43']
sns.set_palette(sns.color_palette(meridian_colors))

class ChartGenerator:
    """
    Generate charts for economic report
    """

    def __init__(self, output_dir: str='reports/charts'):
        """
        Initialize chart generator
        
        Args:
            output_dir: Directory to save charts
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.setup_korean_font()

    def setup_korean_font(self):
        """
        Setup Korean font for matplotlib
        """
        try:
            font_path = _PROJECT_ROOT / 'assets' / 'fonts' / 'NanumGothic-Regular.ttf'
            if font_path.exists():
                fm.fontManager.addfont(str(font_path))
                plt.rcParams['font.family'] = 'NanumGothic'
            else:
                logger.warning(f'Local NanumGothic not found at {font_path}')
                korean_fonts = ['AppleGothic', 'NanumGothic', 'Malgun Gothic']
                available_fonts = [f.name for f in fm.fontManager.ttflist]
                for font in korean_fonts:
                    if font in available_fonts:
                        plt.rcParams['font.family'] = font
                        break
            plt.rcParams['axes.unicode_minus'] = False
        except Exception as e:
            logger.error(f'Error setting up Korean font: {e}')

    def plot_time_series(self, data: pd.DataFrame, title: str, ylabel: str='Value', filename: str='chart.png', figsize: Tuple[int, int]=(12, 6)) -> str:
        """
        Plot time series data
        
        Args:
            data: DataFrame with time series data
            title: Chart title
            ylabel: Y-axis label
            filename: Output filename
            figsize: Figure size
        """
        try:
            fig, ax = plt.subplots(figsize=figsize)
            for column in data.columns:
                ax.plot(data.index, data[column], label=column, linewidth=2)
            ax.set_title(title, fontsize=16, fontweight='bold')
            ax.set_xlabel('Date', fontsize=12)
            ax.set_ylabel(ylabel, fontsize=12)
            ax.legend(loc='best')
            ax.grid(True, alpha=0.3)
            plt.tight_layout()
            output_path = self.output_dir / filename
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            logger.info(f'✓ Time series chart saved: {output_path}')
            return str(output_path)
        except Exception as e:
            logger.error(f'Error plotting time series: {e}')
            return ''

    def plot_comparison(self, data: Dict[str, pd.Series], title: str, ylabel: str='Value', filename: str='comparison.png', figsize: Tuple[int, int]=(12, 6)) -> str:
        """
        Plot comparison of multiple series
        
        Args:
            data: Dictionary of {label: series}
            title: Chart title
            ylabel: Y-axis label
            filename: Output filename
        """
        try:
            fig, ax = plt.subplots(figsize=figsize)
            for label, series in data.items():
                ax.plot(series.index, series.values, label=label, linewidth=2, marker='o', markersize=4)
            ax.set_title(title, fontsize=16, fontweight='bold')
            ax.set_xlabel('Date', fontsize=12)
            ax.set_ylabel(ylabel, fontsize=12)
            ax.legend(loc='best')
            ax.grid(True, alpha=0.3)
            plt.tight_layout()
            output_path = self.output_dir / filename
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            logger.info(f'✓ Comparison chart saved: {output_path}')
            return str(output_path)
        except Exception as e:
            logger.error(f'Error plotting comparison: {e}')
            return ''

    def plot_correlation_heatmap(self, data: pd.DataFrame, title: str='Correlation Heatmap', filename: str='correlation_heatmap.png', figsize: Tuple[int, int]=(14, 12)) -> str:
        """
        Plot correlation heatmap
        
        Args:
            data: DataFrame with multiple columns
            title: Chart title
            filename: Output filename
        """
        try:
            corr = data.corr()
            fig, ax = plt.subplots(figsize=figsize)
            sns.heatmap(corr, annot=True, fmt='.2f', cmap='coolwarm', center=0, square=True, linewidths=1, cbar_kws={'shrink': 0.8}, ax=ax)
            ax.set_title(title, fontsize=16, fontweight='bold', pad=20)
            plt.tight_layout()
            output_path = self.output_dir / filename
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            logger.info(f'✓ Correlation heatmap saved: {output_path}')
            return str(output_path)
        except Exception as e:
            logger.error(f'Error plotting correlation heatmap: {e}')
            return ''

    def plot_bar_chart(self, data: pd.Series, title: str, xlabel: str='Category', ylabel: str='Value', filename: str='bar_chart.png', figsize: Tuple[int, int]=(10, 6)) -> str:
        """
        Plot bar chart
        
        Args:
            data: Series with data
            title: Chart title
            xlabel: X-axis label
            ylabel: Y-axis label
            filename: Output filename
        """
        try:
            fig, ax = plt.subplots(figsize=figsize)
            colors = [meridian_colors[i % len(meridian_colors)] for i in range(len(data))]
            ax.bar(range(len(data)), data.values, color=colors, edgecolor='none')
            ax.set_xticks(range(len(data)))
            ax.set_xticklabels(data.index, rotation=45, ha='right')
            ax.set_title(title, fontsize=16, fontweight='bold')
            ax.set_xlabel(xlabel, fontsize=12)
            ax.set_ylabel(ylabel, fontsize=12)
            ax.grid(True, alpha=0.3, axis='y')
            plt.tight_layout()
            output_path = self.output_dir / filename
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            logger.info(f'✓ Bar chart saved: {output_path}')
            return str(output_path)
        except Exception as e:
            logger.error(f'Error plotting bar chart: {e}')
            return ''

    def plot_pie_chart(self, data: pd.Series, title: str, filename: str='pie_chart.png', figsize: Tuple[int, int]=(10, 8)) -> str:
        """
        Plot pie chart (for portfolio allocation)
        
        Args:
            data: Series with allocation data
            title: Chart title
            filename: Output filename
        """
        try:
            fig, ax = plt.subplots(figsize=figsize)
            colors = [meridian_colors[i % len(meridian_colors)] for i in range(len(data))]
            wedges, texts, autotexts = ax.pie(data.values, labels=data.index, autopct='%1.1f%%', colors=colors, startangle=90, textprops={'fontsize': 10, 'fontweight': 'bold'}, wedgeprops={'edgecolor': 'white', 'linewidth': 1.5})
            for autotext in autotexts:
                autotext.set_color('white')
                autotext.set_fontweight('bold')
            ax.set_title(title, fontsize=16, fontweight='bold', pad=20)
            plt.tight_layout()
            output_path = self.output_dir / filename
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            logger.info(f'✓ Pie chart saved: {output_path}')
            return str(output_path)
        except Exception as e:
            logger.error(f'Error plotting pie chart: {e}')
            return ''

    def plot_efficient_frontier(self, returns: np.ndarray, risks: np.ndarray, sharpe_ratios: np.ndarray, max_sharpe_idx: int, min_vol_idx: int, title: str='Efficient Frontier', filename: str='efficient_frontier.png', figsize: Tuple[int, int]=(12, 8)) -> str:
        """
        Plot efficient frontier
        
        Args:
            returns: Array of portfolio returns
            risks: Array of portfolio risks
            sharpe_ratios: Array of Sharpe ratios
            max_sharpe_idx: Index of maximum Sharpe ratio portfolio
            min_vol_idx: Index of minimum volatility portfolio
            title: Chart title
            filename: Output filename
        """
        try:
            fig, ax = plt.subplots(figsize=figsize)
            scatter = ax.scatter(risks, returns, c=sharpe_ratios, cmap='viridis', marker='o', s=50, alpha=0.6, edgecolors='black', linewidth=0.5)
            ax.scatter(risks[max_sharpe_idx], returns[max_sharpe_idx], marker='*', color='red', s=500, edgecolors='black', linewidth=2, label='Max Sharpe Ratio', zorder=5)
            ax.scatter(risks[min_vol_idx], returns[min_vol_idx], marker='*', color='green', s=500, edgecolors='black', linewidth=2, label='Min Volatility', zorder=5)
            cbar = plt.colorbar(scatter, ax=ax)
            cbar.set_label('Sharpe Ratio', fontsize=12)
            ax.set_title(title, fontsize=16, fontweight='bold')
            ax.set_xlabel('Risk (Volatility)', fontsize=12)
            ax.set_ylabel('Expected Return', fontsize=12)
            ax.legend(loc='best', fontsize=10)
            ax.grid(True, alpha=0.3)
            plt.tight_layout()
            output_path = self.output_dir / filename
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            logger.info(f'✓ Efficient frontier chart saved: {output_path}')
            return str(output_path)
        except Exception as e:
            logger.error(f'Error plotting efficient frontier: {e}')
            return ''

    def plot_forecast(self, historical: pd.Series, forecast: pd.Series, confidence_interval: Optional[Tuple[pd.Series, pd.Series]]=None, title: str='Forecast', filename: str='forecast.png', figsize: Tuple[int, int]=(14, 7)) -> str:
        """
        Plot forecast with confidence interval
        
        Args:
            historical: Historical data
            forecast: Forecast data
            confidence_interval: Tuple of (lower_bound, upper_bound)
            title: Chart title
            filename: Output filename
        """
        try:
            fig, ax = plt.subplots(figsize=figsize)
            ax.plot(historical.index, historical.values, label='Historical', color='blue', linewidth=2)
            ax.plot(forecast.index, forecast.values, label='Forecast', color='red', linewidth=2, linestyle='--')
            if confidence_interval:
                try:
                    if isinstance(confidence_interval, tuple) and len(confidence_interval) == 2:
                        lower, upper = confidence_interval
                        ax.fill_between(forecast.index, lower.values, upper.values, alpha=0.3, color='red', label='95% Confidence Interval')
                    else:
                        logger.warning(f'Skipping confidence interval - unexpected format: {type(confidence_interval)}')
                except Exception as ci_error:
                    logger.warning(f'Could not plot confidence interval: {ci_error}')
            ax.set_title(title, fontsize=16, fontweight='bold')
            ax.set_xlabel('Date', fontsize=12)
            ax.set_ylabel('Value', fontsize=12)
            ax.legend(loc='best')
            ax.grid(True, alpha=0.3)
            ax.axvline(x=historical.index[-1], color='gray', linestyle=':', linewidth=2)
            plt.tight_layout()
            output_path = self.output_dir / filename
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            logger.info(f'✓ Forecast chart saved: {output_path}')
            return str(output_path)
        except Exception as e:
            logger.error(f'Error plotting forecast: {e}')
            return ''

    def plot_interactive_time_series(self, data: pd.DataFrame, title: str, filename: str='interactive_chart.html') -> str:
        """
        Create interactive time series chart using Plotly
        
        Args:
            data: DataFrame with time series data
            title: Chart title
            filename: Output filename
        """
        try:
            fig = go.Figure()
            for column in data.columns:
                fig.add_trace(go.Scatter(x=data.index, y=data[column], mode='lines', name=column, line=dict(width=2)))
            fig.update_layout(title=title, xaxis_title='Date', yaxis_title='Value', hovermode='x unified', template='plotly_white', height=600)
            output_path = self.output_dir / filename
            fig.write_html(str(output_path))
            logger.info(f'✓ Interactive chart saved: {output_path}')
            return str(output_path)
        except Exception as e:
            logger.error(f'Error creating interactive chart: {e}')
            return ''

    def plot_monte_carlo(self, simulations: np.ndarray, percentiles: Dict[str, float], initial_value: float, title: str='Monte Carlo Simulation', filename: str='monte_carlo.png', figsize: Tuple[int, int]=(14, 8)) -> str:
        """
        Plot Monte Carlo simulation results
        
        Args:
            simulations: Array of simulation results (num_simulations x num_days)
            percentiles: Dictionary of percentile values
            initial_value: Initial investment value
            title: Chart title
            filename: Output filename
        """
        try:
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=figsize)
            num_simulations = simulations.shape[0]
            num_days = simulations.shape[1]
            num_to_plot = min(100, num_simulations)
            indices = np.random.choice(num_simulations, num_to_plot, replace=False)
            for idx in indices:
                ax1.plot(simulations[idx], color='blue', alpha=0.1, linewidth=0.5)
            p5 = np.percentile(simulations, 5, axis=0)
            p50 = np.percentile(simulations, 50, axis=0)
            p95 = np.percentile(simulations, 95, axis=0)
            ax1.plot(p50, color='red', linewidth=2, label='Median (50th percentile)')
            ax1.plot(p5, color='orange', linewidth=2, linestyle='--', label='5th percentile')
            ax1.plot(p95, color='green', linewidth=2, linestyle='--', label='95th percentile')
            ax1.axhline(y=initial_value, color='black', linestyle=':', linewidth=2, label='Initial Value')
            ax1.set_title(f'{title} - Portfolio Value Over Time', fontsize=14, fontweight='bold')
            ax1.set_xlabel('Days', fontsize=12)
            ax1.set_ylabel('Portfolio Value ($)', fontsize=12)
            ax1.legend(loc='best')
            ax1.grid(True, alpha=0.3)
            final_values = simulations[:, -1]
            ax2.hist(final_values, bins=50, color='skyblue', edgecolor='black', alpha=0.7)
            ax2.axvline(x=initial_value, color='black', linestyle=':', linewidth=2, label='Initial Value')
            ax2.axvline(x=percentiles.get('percentile_50', np.median(final_values)), color='red', linewidth=2, label='Median')
            ax2.axvline(x=percentiles.get('percentile_5', np.percentile(final_values, 5)), color='orange', linewidth=2, linestyle='--', label='5th percentile')
            ax2.axvline(x=percentiles.get('percentile_95', np.percentile(final_values, 95)), color='green', linewidth=2, linestyle='--', label='95th percentile')
            ax2.set_title('Distribution of Final Portfolio Values', fontsize=14, fontweight='bold')
            ax2.set_xlabel('Final Portfolio Value ($)', fontsize=12)
            ax2.set_ylabel('Frequency', fontsize=12)
            ax2.legend(loc='best')
            ax2.grid(True, alpha=0.3, axis='y')
            plt.tight_layout()
            output_path = self.output_dir / filename
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            logger.info(f'✓ Monte Carlo chart saved: {output_path}')
            return str(output_path)
        except Exception as e:
            logger.error(f'Error plotting Monte Carlo simulation: {e}')
            return ''