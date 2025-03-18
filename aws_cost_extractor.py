import boto3
import datetime
import pandas as pd
import os
from dateutil.relativedelta import relativedelta
import argparse
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter


class AWSCostExtractor:
    def __init__(self, profile_name=None):
        """
        Inicializa o extrator de custos AWS.
        
        Args:
            profile_name: Nome do perfil AWS configurado localmente (opcional)
        """
        self.session = boto3.Session(profile_name=profile_name)
        self.sts_client = self.session.client('sts')
        self.organizations_client = self.session.client('organizations')
        self.account_data = []
        
    def get_aws_accounts(self):
        """
        Obtém todas as contas AWS na organização.
        
        Returns:
            list: Lista de dicionários com informações das contas
        """
        accounts = []
        try:
            paginator = self.organizations_client.get_paginator('list_accounts')
            for page in paginator.paginate():
                accounts.extend(page['Accounts'])
            return accounts
        except Exception as e:
            print(f"Erro ao obter contas: {str(e)}")
            return []
            
    def assume_role(self, account_id, role_name="OrganizationAccountAccessRole"):
        """
        Assume uma função em uma conta específica para acessar recursos.
        
        Args:
            account_id: ID da conta AWS
            role_name: Nome da função a ser assumida
            
        Returns:
            boto3.Session: Uma sessão com as credenciais temporárias
        """
        try:
            role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
            response = self.sts_client.assume_role(
                RoleArn=role_arn,
                RoleSessionName="CostExplorerExtractionSession"
            )
            
            credentials = response['Credentials']
            return boto3.Session(
                aws_access_key_id=credentials['AccessKeyId'],
                aws_secret_access_key=credentials['SecretAccessKey'],
                aws_session_token=credentials['SessionToken']
            )
        except Exception as e:
            print(f"Erro ao assumir função na conta {account_id}: {str(e)}")
            return None
            
    def get_cost_data(self, account_id, account_name):
        """
        Obtém dados de custo para uma conta específica.
        
        Args:
            account_id: ID da conta AWS
            account_name: Nome da conta AWS
            
        Returns:
            dict: Dicionário com os dados de custo
        """
        try:
            account_session = self.assume_role(account_id)
            if not account_session:
                return None
                
            cost_explorer = account_session.client('ce')
            
            # Define o período dos últimos 3 meses
            end_date = datetime.datetime.now().date().replace(day=1)
            start_date = (end_date - relativedelta(months=3)).replace(day=1)
            
            start_str = start_date.strftime('%Y-%m-%d')
            end_str = end_date.strftime('%Y-%m-%d')
            
            # Obter custo total
            total_cost_response = cost_explorer.get_cost_and_usage(
                TimePeriod={
                    'Start': start_str,
                    'End': end_str
                },
                Granularity='MONTHLY',
                Metrics=['UnblendedCost']
            )
            
            # Obter custo do CloudWatch
            cloudwatch_cost_response = cost_explorer.get_cost_and_usage(
                TimePeriod={
                    'Start': start_str,
                    'End': end_str
                },
                Granularity='MONTHLY',
                Filter={
                    "Dimensions": {
                        "Key": "SERVICE",
                        "Values": ["Amazon CloudWatch"]
                    }
                },
                Metrics=['UnblendedCost']
            )
            
            # Processar resultados
            months = []
            total_costs = []
            cloudwatch_costs = []
            percentages = []
            
            # Extrair custos totais por mês
            for result in total_cost_response['ResultsByTime']:
                period = result['TimePeriod']
                month = datetime.datetime.strptime(period['Start'], '%Y-%m-%d').strftime('%b-%Y')
                months.append(month)
                
                amount = float(result['Total']['UnblendedCost']['Amount'])
                total_costs.append(amount)
            
            # Extrair custos do CloudWatch por mês
            for result in cloudwatch_cost_response['ResultsByTime']:
                if 'Total' in result and 'UnblendedCost' in result['Total']:
                    amount = float(result['Total']['UnblendedCost']['Amount'])
                else:
                    amount = 0.0
                cloudwatch_costs.append(amount)
            
            # Calcular percentuais
            for total, cloudwatch in zip(total_costs, cloudwatch_costs):
                if total > 0:
                    percentage = (cloudwatch / total) * 100
                else:
                    percentage = 0.0
                percentages.append(percentage)
            
            # Calcular totais para os 3 meses
            total_3_months = sum(total_costs)
            cloudwatch_3_months = sum(cloudwatch_costs)
            percentage_3_months = (cloudwatch_3_months / total_3_months * 100) if total_3_months > 0 else 0.0
            
            return {
                'account_id': account_id,
                'account_name': account_name,
                'months': months,
                'total_costs': total_costs,
                'cloudwatch_costs': cloudwatch_costs,
                'percentages': percentages,
                'total_3_months': total_3_months,
                'cloudwatch_3_months': cloudwatch_3_months,
                'percentage_3_months': percentage_3_months
            }
            
        except Exception as e:
            print(f"Erro ao obter dados de custo para a conta {account_id}: {str(e)}")
            return None
    
    def extract_all_accounts_cost(self):
        """
        Extrai dados de custo para todas as contas.
        """
        accounts = self.get_aws_accounts()
        total_accounts = len(accounts)
        
        print(f"Encontradas {total_accounts} contas. Iniciando extração de custos...")
        
        for i, account in enumerate(accounts, 1):
            account_id = account['Id']
            account_name = account['Name']
            print(f"Processando conta {i}/{total_accounts}: {account_name} ({account_id})")
            
            cost_data = self.get_cost_data(account_id, account_name)
            if cost_data:
                self.account_data.append(cost_data)
                print(f"✅ Dados extraídos com sucesso para {account_name}")
            else:
                print(f"❌ Falha ao extrair dados para {account_name}")
        
        print(f"Extração concluída para {len(self.account_data)} de {total_accounts} contas.")
    
    def generate_excel_report(self, output_path="aws_cost_report.xlsx"):
        """
        Gera um relatório Excel com os dados de custo extraídos.
        
        Args:
            output_path: Caminho para salvar o arquivo Excel
        """
        if not self.account_data:
            print("Sem dados para gerar relatório.")
            return
        
        # Criar DataFrame para o resumo das contas
        summary_data = []
        for account in self.account_data:
            summary_data.append({
                'ID da Conta': account['account_id'],
                'Nome da Conta': account['account_name'],
                'Custo Total (USD)': account['total_3_months'],
                'Custo CloudWatch (USD)': account['cloudwatch_3_months'],
                'Percentual CloudWatch (%)': account['percentage_3_months']
            })
        
        summary_df = pd.DataFrame(summary_data)
        
        # Criar DataFrames para os detalhes mensais
        monthly_data = []
        for account in self.account_data:
            for i, month in enumerate(account['months']):
                monthly_data.append({
                    'ID da Conta': account['account_id'],
                    'Nome da Conta': account['account_name'],
                    'Mês': month,
                    'Custo Total (USD)': account['total_costs'][i],
                    'Custo CloudWatch (USD)': account['cloudwatch_costs'][i],
                    'Percentual CloudWatch (%)': account['percentages'][i]
                })
        
        monthly_df = pd.DataFrame(monthly_data)
        
        # Ordenar os DataFrames
        summary_df = summary_df.sort_values('Custo Total (USD)', ascending=False)
        monthly_df = monthly_df.sort_values(['Nome da Conta', 'Mês'])
        
        # Criar arquivo Excel
        with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
            summary_df.to_excel(writer, sheet_name='Resumo', index=False)
            monthly_df.to_excel(writer, sheet_name='Detalhes Mensais', index=False)
            
            # Formatar as planilhas
            workbook = writer.book
            
            # Formatar a planilha de resumo
            worksheet = writer.sheets['Resumo']
            
            # Definir largura das colunas
            for idx, col in enumerate(summary_df.columns):
                column_width = max(len(col) + 2, summary_df[col].astype(str).map(len).max() + 2)
                worksheet.column_dimensions[get_column_letter(idx + 1)].width = column_width
            
            # Formatar células numéricas
            for row in range(2, len(summary_df) + 2):
                worksheet.cell(row=row, column=3).number_format = '$#,##0.00'
                worksheet.cell(row=row, column=4).number_format = '$#,##0.00'
                worksheet.cell(row=row, column=5).number_format = '0.00%'
                
                # Destacar percentuais altos de CloudWatch (> 10%)
                percentage = worksheet.cell(row=row, column=5).value
                if percentage and percentage > 10:
                    worksheet.cell(row=row, column=5).fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
            
            # Formatar cabeçalhos
            header_font = Font(bold=True)
            header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
            header_alignment = Alignment(horizontal='center')
            
            for col in range(1, len(summary_df.columns) + 1):
                cell = worksheet.cell(row=1, column=col)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment
            
            # Formatar a planilha de detalhes mensais
            worksheet = writer.sheets['Detalhes Mensais']
            
            # Definir largura das colunas
            for idx, col in enumerate(monthly_df.columns):
                column_width = max(len(col) + 2, monthly_df[col].astype(str).map(len).max() + 2)
                worksheet.column_dimensions[get_column_letter(idx + 1)].width = column_width
            
            # Formatar células numéricas
            for row in range(2, len(monthly_df) + 2):
                worksheet.cell(row=row, column=4).number_format = '$#,##0.00'
                worksheet.cell(row=row, column=5).number_format = '$#,##0.00'
                worksheet.cell(row=row, column=6).number_format = '0.00%'
            
            # Formatar cabeçalhos
            for col in range(1, len(monthly_df.columns) + 1):
                cell = worksheet.cell(row=1, column=col)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment
        
        print(f"Relatório gerado com sucesso: {output_path}")
    
    def generate_html_report(self, output_path="aws_cost_report.html"):
        """
        Gera um relatório HTML com os dados de custo extraídos.
        
        Args:
            output_path: Caminho para salvar o arquivo HTML
        """
        if not self.account_data:
            print("Sem dados para gerar relatório.")
            return
        
        # Criar DataFrame para o resumo das contas
        summary_data = []
        for account in self.account_data:
            summary_data.append({
                'ID da Conta': account['account_id'],
                'Nome da Conta': account['account_name'],
                'Custo Total (USD)': account['total_3_months'],
                'Custo CloudWatch (USD)': account['cloudwatch_3_months'],
                'Percentual CloudWatch (%)': account['percentage_3_months']
            })
        
        summary_df = pd.DataFrame(summary_data)
        
        # Criar DataFrames para os detalhes mensais
        monthly_data = []
        for account in self.account_data:
            for i, month in enumerate(account['months']):
                monthly_data.append({
                    'ID da Conta': account['account_id'],
                    'Nome da Conta': account['account_name'],
                    'Mês': month,
                    'Custo Total (USD)': account['total_costs'][i],
                    'Custo CloudWatch (USD)': account['cloudwatch_costs'][i],
                    'Percentual CloudWatch (%)': account['percentages'][i]
                })
        
        monthly_df = pd.DataFrame(monthly_data)
        
        # Ordenar os DataFrames
        summary_df = summary_df.sort_values('Custo Total (USD)', ascending=False)
        monthly_df = monthly_df.sort_values(['Nome da Conta', 'Mês'])
        
        # Formatar os dados para exibição HTML
        summary_df['Custo Total (USD)'] = summary_df['Custo Total (USD)'].map('${:,.2f}'.format)
        summary_df['Custo CloudWatch (USD)'] = summary_df['Custo CloudWatch (USD)'].map('${:,.2f}'.format)
        summary_df['Percentual CloudWatch (%)'] = summary_df['Percentual CloudWatch (%)'].map('{:.2f}%'.format)
        
        monthly_df['Custo Total (USD)'] = monthly_df['Custo Total (USD)'].map('${:,.2f}'.format)
        monthly_df['Custo CloudWatch (USD)'] = monthly_df['Custo CloudWatch (USD)'].map('${:,.2f}'.format)
        monthly_df['Percentual CloudWatch (%)'] = monthly_df['Percentual CloudWatch (%)'].map('{:.2f}%'.format)
        
        # Criar HTML
        html_content = f"""
        <!DOCTYPE html>
        <html lang="pt-br">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Relatório de Custos AWS</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    margin: 0;
                    padding: 20px;
                    color: #333;
                }}
                h1, h2 {{
                    color: #0066cc;
                }}
                .container {{
                    max-width: 1200px;
                    margin: 0 auto;
                }}
                table {{
                    border-collapse: collapse;
                    width: 100%;
                    margin-bottom: 30px;
                }}
                th, td {{
                    text-align: left;
                    padding: 12px;
                    border-bottom: 1px solid #ddd;
                }}
                th {{
                    background-color: #0066cc;
                    color: white;
                }}
                tr:hover {{
                    background-color: #f5f5f5;
                }}
                .high-percentage {{
                    background-color: #FFEB9C;
                }}
                .timestamp {{
                    font-size: 0.8em;
                    color: #666;
                    margin-bottom: 20px;
                }}
                .summary {{
                    background-color: #f0f7ff;
                    padding: 15px;
                    border-radius: 5px;
                    margin-bottom: 20px;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Relatório de Custos AWS</h1>
                <div class="timestamp">Gerado em: {datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}</div>
                
                <div class="summary">
                    <h2>Resumo dos Últimos 3 Meses</h2>
                    <p>Total de Contas Analisadas: {len(summary_df)}</p>
                </div>
                
                <h2>Custos por Conta</h2>
                {summary_df.to_html(index=False, classes="table")}
                
                <h2>Detalhes Mensais</h2>
                {monthly_df.to_html(index=False, classes="table")}
            </div>
            
            <script>
                // Destacar percentuais altos de CloudWatch (> 10%)
                document.addEventListener('DOMContentLoaded', function() {{
                    const tables = document.querySelectorAll('table');
                    tables.forEach(table => {{
                        const headerRow = table.querySelector('tr');
                        const headers = headerRow.querySelectorAll('th');
                        
                        let percentColIndex = -1;
                        for (let i = 0; i < headers.length; i++) {{
                            if (headers[i].textContent.includes('Percentual CloudWatch')) {{
                                percentColIndex = i;
                                break;
                            }}
                        }}
                        
                        if (percentColIndex >= 0) {{
                            const rows = table.querySelectorAll('tr');
                            for (let i = 1; i < rows.length; i++) {{
                                const cell = rows[i].querySelectorAll('td')[percentColIndex];
                                const percentValue = parseFloat(cell.textContent);
                                if (percentValue > 10) {{
                                    cell.classList.add('high-percentage');
                                }}
                            }}
                        }}
                    }});
                }});
            </script>
        </body>
        </html>
        """
        
        # Salvar o arquivo HTML
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        print(f"Relatório HTML gerado com sucesso: {output_path}")


def main():
    parser = argparse.ArgumentParser(description='Extrai dados de custo da AWS para múltiplas contas.')
    parser.add_argument('--profile', type=str, help='Nome do perfil AWS configurado localmente')
    parser.add_argument('--format', choices=['excel', 'html', 'both'], default='both', 
                        help='Formato do relatório: excel, html, ou both (ambos)')
    parser.add_argument('--output-excel', type=str, default='aws_cost_report.xlsx',
                        help='Caminho para salvar o relatório Excel')
    parser.add_argument('--output-html', type=str, default='aws_cost_report.html',
                        help='Caminho para salvar o relatório HTML')
    
    args = parser.parse_args()
    
    extractor = AWSCostExtractor(profile_name=args.profile)
    
    print("Iniciando extração de custos da AWS...")
    extractor.extract_all_accounts_cost()
    
    if args.format in ['excel', 'both']:
        extractor.generate_excel_report(output_path=args.output_excel)
    
    if args.format in ['html', 'both']:
        extractor.generate_html_report(output_path=args.output_html)
    
    print("Processo concluído!")


if __name__ == "__main__":
    main()