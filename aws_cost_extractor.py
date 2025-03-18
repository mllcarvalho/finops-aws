import boto3
import datetime
import pandas as pd
import os
import sys
import json
import webbrowser
import time
from botocore.exceptions import ClientError
from dateutil.relativedelta import relativedelta
import argparse
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter


class AWSSSOAuth:
    """
    Classe para autenticação via AWS SSO e acesso às contas.
    """
    def __init__(self, start_url, region="us-east-1", sso_region=None):
        """
        Inicializa o autenticador SSO.
        
        Args:
            start_url: URL do portal SSO (ex: itaulzprod.awsapps.com/start)
            region: Região principal para API calls
            sso_region: Região do SSO (se diferente da principal)
        """
        self.start_url = start_url if start_url.startswith('https://') else f'https://{start_url}'
        self.region = region
        self.sso_region = sso_region if sso_region else region
        self.session = boto3.Session(region_name=self.region)
        self.sso_oidc_client = self.session.client('sso-oidc', region_name=self.sso_region)
        self.sso_client = self.session.client('sso', region_name=self.sso_region)
        self.token_cache_file = os.path.expanduser('~/.aws/sso/cache/token.json')
        self.accounts_cache_file = os.path.expanduser('~/.aws/sso/cache/accounts.json')
        
        # Criar diretório para cache se não existir
        os.makedirs(os.path.dirname(self.token_cache_file), exist_ok=True)
        
    def _get_cached_token(self):
        """
        Obtém token SSO do cache se válido.
        
        Returns:
            dict: Token de acesso ou None
        """
        if os.path.exists(self.token_cache_file):
            try:
                with open(self.token_cache_file, 'r') as f:
                    cache = json.load(f)
                    if 'expiresAt' in cache and 'accessToken' in cache:
                        expires_at = datetime.datetime.fromisoformat(cache['expiresAt'].replace('Z', '+00:00'))
                        if expires_at > datetime.datetime.now(datetime.timezone.utc):
                            return cache['accessToken']
            except Exception as e:
                print(f"Erro ao ler cache de token: {str(e)}")
        return None
        
    def _register_client(self):
        """
        Registra um cliente para o processo de autenticação.
        
        Returns:
            tuple: (client_id, client_secret)
        """
        response = self.sso_oidc_client.register_client(
            clientName='aws-cost-extractor',
            clientType='public'
        )
        return response['clientId'], response['clientSecret']
        
    def _start_device_authorization(self, client_id, client_secret):
        """
        Inicia o fluxo de autorização do dispositivo.
        
        Args:
            client_id: ID do cliente registrado
            client_secret: Secret do cliente registrado
            
        Returns:
            dict: Informações de autorização incluindo o URL para abrir no navegador
        """
        response = self.sso_oidc_client.start_device_authorization(
            clientId=client_id,
            clientSecret=client_secret,
            startUrl=self.start_url
        )
        return response
        
    def _open_browser_for_auth(self, verification_uri_complete):
        """
        Abre o navegador para autenticação.
        
        Args:
            verification_uri_complete: URL completo para autenticação
        """
        print(f"\nAbrindo navegador para autenticação AWS SSO...")
        print(f"Se o navegador não abrir automaticamente, acesse manualmente: {verification_uri_complete}")
        
        try:
            webbrowser.open(verification_uri_complete)
        except Exception as e:
            print(f"Erro ao abrir navegador: {str(e)}")
            print(f"Por favor, abra manualmente: {verification_uri_complete}")
        
        print("\nAguardando autenticação... (autorize no navegador e volte aqui)")
        
    def _get_token(self, client_id, client_secret, device_code):
        """
        Obtém o token após aprovação no navegador.
        
        Args:
            client_id: ID do cliente
            client_secret: Secret do cliente
            device_code: Código do dispositivo
            
        Returns:
            str: Token de acesso
        """
        # Tentar obter o token a cada 5 segundos até a autorização ou timeout
        max_tries = 60  # 5 minutos
        tries = 0
        
        while tries < max_tries:
            try:
                response = self.sso_oidc_client.create_token(
                    clientId=client_id,
                    clientSecret=client_secret,
                    grantType='urn:ietf:params:oauth:grant-type:device_code',
                    deviceCode=device_code
                )
                
                # Salvar token no cache
                cache = {
                    'accessToken': response['accessToken'],
                    'expiresAt': (datetime.datetime.now(datetime.timezone.utc) + 
                                 datetime.timedelta(seconds=response['expiresIn'])).isoformat()
                }
                
                with open(self.token_cache_file, 'w') as f:
                    json.dump(cache, f)
                    
                print("Autenticação bem-sucedida!")
                return response['accessToken']
                
            except self.sso_oidc_client.exceptions.AuthorizationPendingException:
                # Ainda aguardando aprovação
                time.sleep(5)
                tries += 1
            except Exception as e:
                print(f"Erro ao obter token: {str(e)}")
                return None
                
        print("Timeout na autenticação. Por favor, tente novamente.")
        return None
        
    def authenticate(self):
        """
        Executa o fluxo completo de autenticação.
        
        Returns:
            str: Token de acesso
        """
        # Verificar cache primeiro
        token = self._get_cached_token()
        if token:
            print("Usando token SSO em cache.")
            return token
            
        # Iniciar novo fluxo de autenticação
        print("Iniciando fluxo de autenticação AWS SSO...")
        client_id, client_secret = self._register_client()
        
        auth_response = self._start_device_authorization(client_id, client_secret)
        self._open_browser_for_auth(auth_response['verificationUriComplete'])
        
        return self._get_token(client_id, client_secret, auth_response['deviceCode'])
        
    def list_available_accounts(self, access_token):
        """
        Lista todas as contas disponíveis para o usuário.
        
        Args:
            access_token: Token de acesso SSO
            
        Returns:
            list: Lista de dicionários com informações das contas
        """
        # Verificar cache primeiro
        if os.path.exists(self.accounts_cache_file):
            try:
                with open(self.accounts_cache_file, 'r') as f:
                    cache = json.load(f)
                    if 'expiresAt' in cache and 'accounts' in cache:
                        expires_at = datetime.datetime.fromisoformat(cache['expiresAt'].replace('Z', '+00:00'))
                        if expires_at > datetime.datetime.now(datetime.timezone.utc):
                            print("Usando cache de contas disponíveis.")
                            return cache['accounts']
            except Exception as e:
                print(f"Erro ao ler cache de contas: {str(e)}")
        
        accounts = []
        try:
            paginator = self.sso_client.get_paginator('list_accounts')
            
            for page in paginator.paginate(accessToken=access_token):
                accounts.extend(page['accountList'])
                
            # Salvar no cache
            cache = {
                'accounts': accounts,
                'expiresAt': (datetime.datetime.now(datetime.timezone.utc) + 
                             datetime.timedelta(hours=24)).isoformat()
            }
            
            with open(self.accounts_cache_file, 'w') as f:
                json.dump(cache, f)
                
            return accounts
        except Exception as e:
            print(f"Erro ao listar contas: {str(e)}")
            return []
            
    def get_account_roles(self, access_token, account_id):
        """
        Obtém todas as funções disponíveis para uma conta.
        
        Args:
            access_token: Token de acesso SSO
            account_id: ID da conta AWS
            
        Returns:
            list: Lista de funções disponíveis
        """
        try:
            response = self.sso_client.list_account_roles(
                accessToken=access_token,
                accountId=account_id
            )
            return response['roleList']
        except Exception as e:
            print(f"Erro ao listar funções para conta {account_id}: {str(e)}")
            return []
            
    def get_credentials(self, access_token, account_id, role_name):
        """
        Obtém credenciais temporárias para uma conta/função.
        
        Args:
            access_token: Token de acesso SSO
            account_id: ID da conta AWS
            role_name: Nome da função a assumir
            
        Returns:
            boto3.Session: Sessão com as credenciais temporárias
        """
        try:
            response = self.sso_client.get_role_credentials(
                accessToken=access_token,
                accountId=account_id,
                roleName=role_name
            )
            
            creds = response['roleCredentials']
            
            return boto3.Session(
                aws_access_key_id=creds['accessKeyId'],
                aws_secret_access_key=creds['secretAccessKey'],
                aws_session_token=creds['sessionToken'],
                region_name=self.region
            )
        except Exception as e:
            print(f"Erro ao obter credenciais para conta {account_id}, função {role_name}: {str(e)}")
            return None


class AWSCostExtractor:
    def __init__(self, sso_auth, access_token):
        """
        Inicializa o extrator de custos AWS.
        
        Args:
            sso_auth: Instância de AWSSSOAuth
            access_token: Token de acesso SSO
        """
        self.sso_auth = sso_auth
        self.access_token = access_token
        self.account_data = []
        
    def get_cost_data(self, account_id, account_name, role_name):
        """
        Obtém dados de custo para uma conta específica.
        
        Args:
            account_id: ID da conta AWS
            account_name: Nome da conta AWS
            role_name: Nome da função a assumir
            
        Returns:
            dict: Dicionário com os dados de custo
        """
        try:
            account_session = self.sso_auth.get_credentials(
                self.access_token, account_id, role_name
            )
            
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
                'role_name': role_name,
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
    
    def extract_all_accounts_cost(self, role_name_preference=None):
        """
        Extrai dados de custo para todas as contas.
        
        Args:
            role_name_preference: Lista de nomes de funções por ordem de preferência
        """
        accounts = self.sso_auth.list_available_accounts(self.access_token)
        total_accounts = len(accounts)
        
        print(f"Encontradas {total_accounts} contas. Iniciando extração de custos...")
        
        for i, account in enumerate(accounts, 1):
            account_id = account['accountId']
            account_name = account['accountName']
            print(f"Processando conta {i}/{total_accounts}: {account_name} ({account_id})")
            
            # Obter funções disponíveis para a conta
            roles = self.sso_auth.get_account_roles(self.access_token, account_id)
            
            if not roles:
                print(f"⚠️ Nenhuma função disponível para a conta {account_name}")
                continue
                
            # Selecionar função apropriada
            selected_role = None
            
            # Se tiver uma lista de preferências, use-a
            if role_name_preference:
                for preferred_role in role_name_preference:
                    for role in roles:
                        if role['roleName'] == preferred_role:
                            selected_role = preferred_role
                            break
                    if selected_role:
                        break
            
            # Se não tiver preferência ou nenhuma função preferida estiver disponível,
            # use a primeira função com "Admin" ou a primeira disponível
            if not selected_role:
                for role in roles:
                    if 'Admin' in role['roleName'] or 'admin' in role['roleName']:
                        selected_role = role['roleName']
                        break
                        
                if not selected_role and roles:
                    selected_role = roles[0]['roleName']
            
            if not selected_role:
                print(f"⚠️ Não foi possível selecionar uma função para a conta {account_name}")
                continue
                
            print(f"Usando função: {selected_role}")
            
            cost_data = self.get_cost_data(account_id, account_name, selected_role)
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
                'Função': account['role_name'],
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
                worksheet.cell(row=row, column=4).number_format = '$#,##0.00'
                worksheet.cell(row=row, column=5).number_format = '$#,##0.00'
                worksheet.cell(row=row, column=6).number_format = '0.00%'
                
                # Destacar percentuais altos de CloudWatch (> 10%)
                percentage = worksheet.cell(row=row, column=6).value
                if percentage and percentage > 10:
                    worksheet.cell(row=row, column=6).fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
            
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
                'Função': account['role_name'],
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
    parser = argparse.ArgumentParser(description='Extrai dados de custo da AWS para múltiplas contas via SSO.')
    parser.add_argument('--start-url', type=str, required=True,
                        help='URL do portal SSO (ex: itaulzprod.awsapps.com/start)')
    parser.add_argument('--region', type=str, default='us-east-1',
                        help='Região AWS principal (padrão: us-east-1)')
    parser.add_argument('--sso-region', type=str,
                        help='Região do SSO se diferente da região principal')
    parser.add_argument('--preferred-roles', type=str, nargs='+',
                        help='Lista de nomes de funções preferidas