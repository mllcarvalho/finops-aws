import boto3
import datetime
import pandas as pd
import os
import sys
import json
import webbrowser
import time
import re
from botocore.exceptions import ClientError
from dateutil.relativedelta import relativedelta
import argparse
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.dimensions import ColumnDimension, DimensionHolder
from openpyxl.formatting.rule import ColorScaleRule
import locale

# Configurar localidade para formatação de números
try:
    locale.setlocale(locale.LC_ALL, 'pt_BR.UTF-8')
except:
    try:
        locale.setlocale(locale.LC_ALL, 'Portuguese_Brazil.1252')
    except:
        pass  # Fallback para configuração padrão


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
        
        # Serviços a serem analisados
        self.services = {
            'CloudWatch': 'AmazonCloudWatch',
            'DynamoDB': 'Amazon DynamoDB',
            'RDS': 'Amazon Relational Database Service',
            'Config': 'AWS Config',
            'ECS': 'Amazon Elastic Container Service',
            'EC2': 'Amazon Elastic Compute Cloud - Compute'
        }
        
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
            
            # Processar resultados do custo total
            months = []
            month_start_dates = []
            total_costs = []
            
            # Extrair custos totais por mês
            for result in total_cost_response['ResultsByTime']:
                period = result['TimePeriod']
                start_date = period['Start']
                month_start_dates.append(start_date)
                month_obj = datetime.datetime.strptime(start_date, '%Y-%m-%d')
                month_name = month_obj.strftime('%b/%Y')
                months.append(month_name)
                
                amount = float(result['Total']['UnblendedCost']['Amount'])
                total_costs.append(amount)
            
            # Dicionário para armazenar custos de cada serviço
            service_costs = {}
            service_percentages = {}
            
            # Obter custo de cada serviço
            for service_key, service_name in self.services.items():
                try:
                    service_response = cost_explorer.get_cost_and_usage(
                        TimePeriod={
                            'Start': start_str,
                            'End': end_str
                        },
                        Granularity='MONTHLY',
                        Filter={
                            "Dimensions": {
                                "Key": "SERVICE",
                                "Values": [service_name]
                            }
                        },
                        Metrics=['UnblendedCost']
                    )
                    
                    # Processar resultados do serviço
                    service_monthly_costs = []
                    
                    # Alinhar os resultados com os meses
                    for start_date in month_start_dates:
                        found = False
                        for result in service_response['ResultsByTime']:
                            if result['TimePeriod']['Start'] == start_date:
                                if 'Total' in result and 'UnblendedCost' in result['Total']:
                                    amount = float(result['Total']['UnblendedCost']['Amount'])
                                else:
                                    amount = 0.0
                                service_monthly_costs.append(amount)
                                found = True
                                break
                        if not found:
                            service_monthly_costs.append(0.0)
                    
                    service_costs[service_key] = service_monthly_costs
                    
                except Exception as e:
                    print(f"Erro ao obter dados do serviço {service_name} para a conta {account_id}: {str(e)}")
                    service_costs[service_key] = [0.0] * len(months)
            
            # Calcular percentuais para cada serviço
            for service_key in self.services.keys():
                service_percentages[service_key] = []
                for i, total in enumerate(total_costs):
                    if total > 0:
                        percentage = (service_costs[service_key][i] / total) * 100
                    else:
                        percentage = 0.0
                    service_percentages[service_key].append(percentage)
            
            # Calcular totais para os 3 meses
            total_3_months = sum(total_costs)
            
            service_totals = {}
            service_total_percentages = {}
            
            for service_key in self.services.keys():
                service_total = sum(service_costs[service_key])
                service_totals[service_key] = service_total
                
                if total_3_months > 0:
                    service_total_percentage = (service_total / total_3_months) * 100
                else:
                    service_total_percentage = 0.0
                    
                service_total_percentages[service_key] = service_total_percentage
            
            return {
                'account_id': account_id,
                'account_name': account_name,
                'role_name': role_name,
                'months': months,
                'month_start_dates': month_start_dates,
                'total_costs': total_costs,
                'service_costs': service_costs,
                'service_percentages': service_percentages,
                'total_3_months': total_3_months,
                'service_totals': service_totals,
                'service_total_percentages': service_total_percentages
            }
            
        except Exception as e:
            print(f"Erro ao obter dados de custo para a conta {account_id}: {str(e)}")
            return None
    
    def extract_all_accounts_cost(self, account_filter=None, max_accounts=None, role_name_preference=None):
        """
        Extrai dados de custo para todas as contas.
        
        Args:
            account_filter: Filtro para nomes/IDs de contas (regex)
            max_accounts: Número máximo de contas a processar
            role_name_preference: Lista de nomes de funções por ordem de preferência
        """
        accounts = self.sso_auth.list_available_accounts(self.access_token)
        
        # Aplicar filtro se especificado
        if account_filter:
            try:
                filter_regex = re.compile(account_filter, re.IGNORECASE)
                accounts = [account for account in accounts 
                           if filter_regex.search(account['accountName']) or 
                           filter_regex.search(account['accountId'])]
                print(f"Filtro aplicado. {len(accounts)} contas correspondem ao filtro '{account_filter}'.")
            except re.error as e:
                print(f"Erro na expressão regular: {str(e)}. Ignorando filtro.")
        
        # Limitar número de contas se especificado
        if max_accounts and max_accounts > 0 and max_accounts < len(accounts):
            accounts = accounts[:max_accounts]
            print(f"Limitando a {max_accounts} contas para processamento.")
            
        total_accounts = len(accounts)
        
        if total_accounts == 0:
            print("Nenhuma conta encontrada para extrair dados. Verifique seu filtro ou permissões SSO.")
            return
            
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
    
    import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import ColorScaleRule

def generate_excel_report(self, output_path="aws_cost_report.xlsx", columns_order=None):
    """
    Gera um relatório Excel com os dados de custo extraídos.
    
    Args:
        output_path: Caminho para salvar o arquivo Excel
        columns_order: Lista com a ordem desejada das colunas (opcional)
    """
    if not self.account_data:
        print("Sem dados para gerar relatório.")
        return
    
    # Criar DataFrame para o resumo das contas
    summary_data = []
    for account in self.account_data:
        row_data = {
            'ID da Conta': account['account_id'],
            'Nome da Conta': account['account_name'],
            'Função': account['role_name'],
            'Custo Total (USD)': account['total_3_months']
        }
        
        # Adicionar colunas para cada serviço
        for service_key in self.services.keys():
            row_data[f'{service_key} (USD)'] = account['service_totals'][service_key]
            if service_key in ['CloudWatch', 'Config']:
                row_data[f'% {service_key}'] = account['service_total_percentages'][service_key]
        
        summary_data.append(row_data)
    
    summary_df = pd.DataFrame(summary_data)
    
    # Reordenar colunas se especificado
    if columns_order:
        available_columns = summary_df.columns.tolist()
        # Verificar colunas válidas
        valid_columns = [col for col in columns_order if col in available_columns]
        # Adicionar colunas que não foram especificadas, mas estão disponíveis
        remaining_columns = [col for col in available_columns if col not in valid_columns]
        # Criar ordem final
        final_order = valid_columns + remaining_columns
        summary_df = summary_df[final_order]
    
    # Ordenar por custo total
    summary_df = summary_df.sort_values('Custo Total (USD)', ascending=False)
    
    # Criar DataFrames separados para cada mês
    monthly_data_by_month = {}
    
    # Obter todos os meses únicos em ordem cronológica
    all_months = []
    for account in self.account_data:
        for month in account['months']:
            if month not in all_months:
                all_months.append(month)
    
    # Ordenar meses (formato 'Mmm/YYYY')
    all_months.sort(key=lambda x: (int(x.split('/')[1]), ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 
                                                     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'].index(x.split('/')[0])))
    
    # Inicializar dicionários para cada mês
    for month in all_months:
        monthly_data_by_month[month] = []
    
    # Preencher dados para cada mês
    for account in self.account_data:
        for i, month in enumerate(account['months']):
            row_data = {
                'ID da Conta': account['account_id'],
                'Nome da Conta': account['account_name'],
                'Custo Total (USD)': account['total_costs'][i]
            }
            
            # Adicionar colunas para cada serviço
            for service_key in self.services.keys():
                row_data[f'{service_key} (USD)'] = account['service_costs'][service_key][i]
                if service_key in ['CloudWatch', 'Config']:
                    row_data[f'% {service_key}'] = account['service_percentages'][service_key][i]
            
            monthly_data_by_month[month].append(row_data)
    
    # Criar DataFrames para cada mês
    monthly_dfs = {}
    for month, data in monthly_data_by_month.items():
        df = pd.DataFrame(data)
        
        # Reordenar colunas se especificado
        if columns_order:
            available_columns = df.columns.tolist()
            valid_columns = [col for col in columns_order if col in available_columns]
            remaining_columns = [col for col in available_columns if col not in valid_columns]
            final_order = valid_columns + remaining_columns
            df = df[final_order]
        
        # Ordenar por custo total
        df = df.sort_values('Custo Total (USD)', ascending=False)
        monthly_dfs[month] = df
    
    # Criar DataFrame para o detalhamento mensal tradicional (para manter compatibilidade)
    monthly_data = []
    for account in self.account_data:
        for i, month in enumerate(account['months']):
            row_data = {
                'ID da Conta': account['account_id'],
                'Nome da Conta': account['account_name'],
                'Mês': month,
                'Custo Total (USD)': account['total_costs'][i]
            }
            
            # Adicionar colunas para cada serviço
            for service_key in self.services.keys():
                row_data[f'{service_key} (USD)'] = account['service_costs'][service_key][i]
                if service_key in ['CloudWatch', 'Config']:
                    row_data[f'% {service_key}'] = account['service_percentages'][service_key][i]
            
            monthly_data.append(row_data)
    
    monthly_df = pd.DataFrame(monthly_data)
    
    # Reordenar colunas se especificado (para o detalhamento mensal tradicional)
    if columns_order and 'Mês' in monthly_df.columns:
        # Garantir que 'Mês' esteja na posição certa
        mes_index = columns_order.index('Mês') if 'Mês' in columns_order else 2
        available_columns = monthly_df.columns.tolist()
        valid_columns = [col for col in columns_order if col in available_columns]
        
        # Adicionar 'Mês' se não estiver na lista
        if 'Mês' not in valid_columns:
            valid_columns.insert(mes_index, 'Mês')
            
        # Adicionar colunas que não foram especificadas
        remaining_columns = [col for col in available_columns if col not in valid_columns]
        final_order = valid_columns + remaining_columns
        monthly_df = monthly_df[final_order]
    
    # Ordenar o detalhamento mensal tradicional
    monthly_df = monthly_df.sort_values(['Nome da Conta', 'Mês'])
    
    # Criar arquivo Excel
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        summary_df.to_excel(writer, sheet_name='Resumo', index=False)
        
        # Adicionar uma aba para cada mês
        for month, df in monthly_dfs.items():
            # Substituir caracteres inválidos em nomes de abas
            sheet_name = month.replace('/', '-')
            df.to_excel(writer, sheet_name=sheet_name, index=False)
        
        # Adicionar a aba de detalhamento mensal tradicional
        monthly_df.to_excel(writer, sheet_name='Todos os Meses', index=False)
        
        # Formatar as planilhas
        workbook = writer.book
        
        # Estilo padrão para as células
        border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        
        # Formatar a planilha de resumo
        self._format_worksheet(
            worksheet=writer.sheets['Resumo'],
            df=summary_df,
            border=border,
            total_col_idx=summary_df.columns.get_loc('Custo Total (USD)'),
            service_keys=self.services.keys()
        )
        
        # Formatar as planilhas de cada mês
        for month in monthly_dfs.keys():
            sheet_name = month.replace('/', '-')
            self._format_worksheet(
                worksheet=writer.sheets[sheet_name],
                df=monthly_dfs[month],
                border=border,
                total_col_idx=monthly_dfs[month].columns.get_loc('Custo Total (USD)'),
                service_keys=self.services.keys()
            )
        
        # Formatar a planilha de todos os meses
        self._format_worksheet(
            worksheet=writer.sheets['Todos os Meses'],
            df=monthly_df,
            border=border,
            total_col_idx=monthly_df.columns.get_loc('Custo Total (USD)'),
            service_keys=self.services.keys()
        )
        
        # Adicionar filtros a todas as planilhas
        for sheet_name in workbook.sheetnames:
            workbook[sheet_name].auto_filter.ref = workbook[sheet_name].dimensions
    
    print(f"Relatório Excel gerado com sucesso: {output_path}")


def _format_worksheet(self, worksheet, df, border, total_col_idx, service_keys):
    """
    Formata uma planilha Excel.
    
    Args:
        worksheet: Planilha a ser formatada
        df: DataFrame com os dados
        border: Estilo de borda
        total_col_idx: Índice da coluna de custo total
        service_keys: Lista de chaves dos serviços
    """
    # Definir largura das colunas
    for idx, col in enumerate(df.columns):
        column_width = max(len(col) + 2, df[col].astype(str).map(len).max() + 2)
        column_width = min(column_width, 40)  # Limitar largura máxima
        worksheet.column_dimensions[get_column_letter(idx + 1)].width = column_width
    
    # Formatar todas as células com borda
    for row in range(1, len(df) + 2):
        for col in range(1, len(df.columns) + 1):
            cell = worksheet.cell(row=row, column=col)
            cell.border = border
    
    # Formatar células numéricas
    for row in range(2, len(df) + 2):
        # Formatar custo total
        total_col = total_col_idx + 1  # Ajuste para indexação base-1 do openpyxl
        worksheet.cell(row=row, column=total_col).number_format = '$#,##0.00'
        
        # Formatar colunas de serviços
        for col_idx, col_name in enumerate(df.columns, 1):
            if ' (USD)' in col_name:
                worksheet.cell(row=row, column=col_idx).number_format = '$#,##0.00'
            elif '% ' in col_name:
                percent_cell = worksheet.cell(row=row, column=col_idx)
                percent_cell.number_format = '0.00%'
                
                # Destacar percentuais altos (> 10%)
                percentage = percent_cell.value
                if percentage and percentage > 10:
                    percent_cell.fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
    
    # Formatar cabeçalhos
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
    
    for col in range(1, len(df.columns) + 1):
        cell = worksheet.cell(row=1, column=col)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
    
    # Congelar primeira linha
    worksheet.freeze_panes = 'A2'
    
    # Adicionar regras de formatação condicional
    # Destacar valores maiores em tons mais escuros de azul para colunas de custo
    cost_columns = []
    for col_idx, col_name in enumerate(df.columns, 1):
        if ' (USD)' in col_name:
            cost_columns.append(col_idx)
    
    for col_idx in cost_columns:
        col_letter = get_column_letter(col_idx)
        max_row = len(df) + 1
        worksheet.conditional_formatting.add(
            f'{col_letter}2:{col_letter}{max_row}',
            ColorScaleRule(
                start_type='min',
                start_color='EBEDF0',
                end_type='max',
                end_color='4472C4'
            )
        )
    
    def generate_html_report(self, output_path="aws_cost_report.html", columns_order=None):
        """
        Gera um relatório HTML com os dados de custo extraídos.
        
        Args:
            output_path: Caminho para salvar o arquivo HTML
            columns_order: Lista com a ordem desejada das colunas (opcional)
        """
        if not self.account_data:
            print("Sem dados para gerar relatório.")
            return
        
        # Criar DataFrame para o resumo das contas
        summary_data = []
        for account in self.account_data:
            row_data = {
                'ID da Conta': account['account_id'],
                'Nome da Conta': account['account_name'],
                'Função': account['role_name'],
                'Custo Total (USD)': account['total_3_months']
            }
            
            # Adicionar colunas para cada serviço
            for service_key in self.services.keys():
                row_data[f'{service_key} (USD)'] = account['service_totals'][service_key]
                if service_key in ['CloudWatch', 'Config']:
                    row_data[f'% {service_key}'] = account['service_total_percentages'][service_key]
            
            summary_data.append(row_data)
        
        summary_df = pd.DataFrame(summary_data)
        
        # Reordenar colunas se especificado
        if columns_order:
            available_columns = summary_df.columns.tolist()
            # Verificar colunas válidas
            valid_columns = [col for col in columns_order if col in available_columns]
            # Adicionar colunas que não foram especificadas, mas estão disponíveis
            remaining_columns = [col for col in available_columns if col not in valid_columns]
            # Criar ordem final
            final_order = valid_columns + remaining_columns
            summary_df = summary_df[final_order]
        
        # Obter todos os meses únicos em ordem cronológica
        all_months = []
        for account in self.account_data:
            for month in account['months']:
                if month not in all_months:
                    all_months.append(month)
        
        # Ordenar meses (formato 'Mmm/YYYY')
        all_months.sort(key=lambda x: (int(x.split('/')[1]), ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 
                                                        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'].index(x.split('/')[0])))
        
        # Criar DataFrames para cada mês
        monthly_dfs = {}
        
        for month in all_months:
            monthly_data = []
            
            for account in self.account_data:
                if month in account['months']:
                    i = account['months'].index(month)
                    
                    row_data = {
                        'ID da Conta': account['account_id'],
                        'Nome da Conta': account['account_name'],
                        'Custo Total (USD)': account['total_costs'][i]
                    }
                    
                    # Adicionar colunas para cada serviço
                    for service_key in self.services.keys():
                        row_data[f'{service_key} (USD)'] = account['service_costs'][service_key][i]
                        if service_key in ['CloudWatch', 'Config']:
                            row_data[f'% {service_key}'] = account['service_percentages'][service_key][i]
                    
                    monthly_data.append(row_data)
            
            df = pd.DataFrame(monthly_data)
            
            # Reordenar colunas se especificado
            if columns_order:
                available_columns = df.columns.tolist()
                valid_columns = [col for col in columns_order if col in available_columns]
                remaining_columns = [col for col in available_columns if col not in valid_columns]
                final_order = valid_columns + remaining_columns
                df = df[final_order]
            
            # Ordenar por custo total
            df = df.sort_values('Custo Total (USD)', ascending=False)
            monthly_dfs[month] = df
        
        # Criar DataFrame para todos os meses (vista tradicional)
        all_monthly_data = []
        for account in self.account_data:
            for i, month in enumerate(account['months']):
                row_data = {
                    'ID da Conta': account['account_id'],
                    'Nome da Conta': account['account_name'],
                    'Mês': month,
                    'Custo Total (USD)': account['total_costs'][i]
                }
                
                # Adicionar colunas para cada serviço
                for service_key in self.services.keys():
                    row_data[f'{service_key} (USD)'] = account['service_costs'][service_key][i]
                    if service_key in ['CloudWatch', 'Config']:
                        row_data[f'% {service_key}'] = account['service_percentages'][service_key][i]
                
                all_monthly_data.append(row_data)
        
        all_monthly_df = pd.DataFrame(all_monthly_data)
        
        # Reordenar colunas se especificado
        if columns_order and len(all_monthly_df) > 0:
            available_columns = all_monthly_df.columns.tolist()
            # Garantir que 'Mês' esteja na posição certa
            if 'Mês' in available_columns:
                mes_index = columns_order.index('Mês') if 'Mês' in columns_order else 2
                valid_columns = [col for col in columns_order if col in available_columns]
                
                # Adicionar 'Mês' se não estiver na lista
                if 'Mês' not in valid_columns:
                    valid_columns.insert(mes_index, 'Mês')
                    
                # Adicionar colunas que não foram especificadas, mas estão disponíveis
                remaining_columns = [col for col in available_columns if col not in valid_columns]
                final_order = valid_columns + remaining_columns
                all_monthly_df = all_monthly_df[final_order]
        
        # Ordenar por conta e mês
        all_monthly_df = all_monthly_df.sort_values(['Nome da Conta', 'Mês'])
        
        # Ordenar o DataFrame de resumo por custo total
        summary_df = summary_df.sort_values('Custo Total (USD)', ascending=False)
        
        # Formatar os dados para exibição HTML
        summary_df_display = summary_df.copy()
        all_monthly_df_display = all_monthly_df.copy()
        
        # Dicionário para versões formatadas dos DataFrames mensais
        monthly_dfs_display = {}
        
        # Formatar colunas de custo no resumo
        summary_df_display['Custo Total (USD)'] = summary_df_display['Custo Total (USD)'].map('${:,.2f}'.format)
        all_monthly_df_display['Custo Total (USD)'] = all_monthly_df_display['Custo Total (USD)'].map('${:,.2f}'.format)
        
        for service_key in self.services.keys():
            summary_df_display[f'{service_key} (USD)'] = summary_df_display[f'{service_key} (USD)'].map('${:,.2f}'.format)
            all_monthly_df_display[f'{service_key} (USD)'] = all_monthly_df_display[f'{service_key} (USD)'].map('${:,.2f}'.format)
            
            if service_key in ['CloudWatch', 'Config']:
                summary_df_display[f'% {service_key}'] = summary_df_display[f'% {service_key}'].map('{:.2f}%'.format)
                all_monthly_df_display[f'% {service_key}'] = all_monthly_df_display[f'% {service_key}'].map('{:.2f}%'.format)
        
        # Formatar DataFrames mensais
        for month, df in monthly_dfs.items():
            df_display = df.copy()
            df_display['Custo Total (USD)'] = df_display['Custo Total (USD)'].map('${:,.2f}'.format)
            
            for service_key in self.services.keys():
                df_display[f'{service_key} (USD)'] = df_display[f'{service_key} (USD)'].map('${:,.2f}'.format)
                
                if service_key in ['CloudWatch', 'Config']:
                    df_display[f'% {service_key}'] = df_display[f'% {service_key}'].map('{:.2f}%'.format)
            
            monthly_dfs_display[month] = df_display
        
        # Gerar cabeçalhos de tabela
        summary_headers = ''.join([f'<th>{col}</th>' for col in summary_df_display.columns])
        all_monthly_headers = ''.join([f'<th>{col}</th>' for col in all_monthly_df_display.columns])
        
        # Dicionário para armazenar cabeçalhos mensais
        monthly_headers = {}
        for month, df_display in monthly_dfs_display.items():
            monthly_headers[month] = ''.join([f'<th>{col}</th>' for col in df_display.columns])
        
        # Gerar linhas da tabela de resumo
        summary_rows = []
        for _, row in summary_df_display.iterrows():
            cells = []
            for col, value in row.items():
                cell_class = "cost-cell" if "USD" in col or "%" in col else ""
                cells.append(f'<td class="{cell_class}">{value}</td>')
            summary_rows.append('<tr>' + ''.join(cells) + '</tr>')
        summary_table_body = ''.join(summary_rows)
        
        # Gerar linhas da tabela mensal geral
        all_monthly_rows = []
        for _, row in all_monthly_df_display.iterrows():
            cells = []
            for col, value in row.items():
                cell_class = "cost-cell" if "USD" in col or "%" in col else ""
                cells.append(f'<td class="{cell_class}">{value}</td>')
            all_monthly_rows.append('<tr>' + ''.join(cells) + '</tr>')
        all_monthly_table_body = ''.join(all_monthly_rows)
        
        # Gerar linhas das tabelas mensais separadas
        monthly_tables_body = {}
        for month, df_display in monthly_dfs_display.items():
            rows = []
            for _, row in df_display.iterrows():
                cells = []
                for col, value in row.items():
                    cell_class = "cost-cell" if "USD" in col or "%" in col else ""
                    cells.append(f'<td class="{cell_class}">{value}</td>')
                rows.append('<tr>' + ''.join(cells) + '</tr>')
            monthly_tables_body[month] = ''.join(rows)
        
        # Gerar abas para os meses
        month_tabs = []
        for i, month in enumerate(all_months):
            active_class = "" if i > 0 else "active"
            month_id = month.replace('/', '-')
            month_tabs.append(f'<div class="tab month-tab {active_class}" id="tab-{month_id}" onclick="showMonthTab(\'{month_id}\')">{month}</div>')
        month_tabs_html = ''.join(month_tabs)
        
        # Gerar conteúdo das abas mensais
        month_contents = []
        for i, month in enumerate(all_months):
            month_id = month.replace('/', '-')
            display_style = "block" if i == 0 else "none"
            month_contents.append(f'''
            <div id="month-{month_id}" class="month-content" style="display: {display_style};">
                <div style="overflow-x: auto;">
                    <table id="table-{month_id}">
                        <thead>
                            <tr>
                                {monthly_headers[month]}
                            </tr>
                        </thead>
                        <tbody>
                            {monthly_tables_body[month]}
                        </tbody>
                    </table>
                </div>
            </div>''')
        month_contents_html = ''.join(month_contents)
        
        # Gerar legenda de serviços
        service_legend = []
        for service_key in self.services.keys():
            color = f'#{abs(hash(service_key)) % 0xffffff:06x}'
            service_legend.append(f'<div class="service-item"><div class="service-color" style="background-color: {color}"></div>{service_key}</div>')
        service_legend_html = ''.join(service_legend)
        
        # Gerar timestamp
        timestamp = datetime.datetime.now().strftime('%d/%m/%Y %H:%M:%S')
        
        # Combinar tudo em um HTML
        html_content = f"""<!DOCTYPE html>
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
            h1, h2, h3 {{
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
                font-size: 14px;
            }}
            th, td {{
                text-align: left;
                padding: 10px;
                border: 1px solid #ddd;
            }}
            th {{
                background-color: #0066cc;
                color: white;
                position: sticky;
                top: 0;
                z-index: 10;
            }}
            tr:nth-child(even) {{
                background-color: #f5f5f5;
            }}
            tr:hover {{
                background-color: #e9f1fa;
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
            .tabs {{
                display: flex;
                margin-bottom: 20px;
                border-bottom: 1px solid #ccc;
            }}
            .tab {{
                padding: 10px 20px;
                background-color: #ddd;
                cursor: pointer;
                border: 1px solid #ccc;
                border-bottom: none;
                border-radius: 5px 5px 0 0;
                margin-right: 5px;
            }}
            .tab.active {{
                background-color: #0066cc;
                color: white;
            }}
            .tab-content {{
                display: none;
            }}
            .tab-content.active {{
                display: block;
            }}
            .month-tabs {{
                display: flex;
                flex-wrap: wrap;
                margin-bottom: 20px;
                border-bottom: 1px solid #ccc;
            }}
            .month-tab {{
                padding: 8px 16px;
                background-color: #eee;
                cursor: pointer;
                border: 1px solid #ddd;
                border-bottom: none;
                border-radius: 4px 4px 0 0;
                margin-right: 4px;
                margin-bottom: 0;
                font-size: 0.9em;
            }}
            .month-tab.active {{
                background-color: #4a86e8;
                color: white;
            }}
            .month-content {{
                margin-top: 20px;
            }}
            .search {{
                padding: 10px;
                margin-bottom: 20px;
                width: 100%;
                box-sizing: border-box;
                border: 1px solid #ddd;
                border-radius: 4px;
            }}
            .cost-cell {{
                text-align: right;
            }}
            .service-legend {{
                margin-bottom: 20px;
                display: flex;
                flex-wrap: wrap;
            }}
            .service-item {{
                margin-right: 20px;
                margin-bottom: 10px;
                display: flex;
                align-items: center;
            }}
            .service-color {{
                width: 20px;
                height: 20px;
                margin-right: 5px;
                border-radius: 3px;
            }}
            .dashboard {{
                display: flex;
                flex-wrap: wrap;
                gap: 20px;
                margin-bottom: 20px;
            }}
            .dashboard-card {{
                background-color: #fff;
                border: 1px solid #ddd;
                border-radius: 5px;
                padding: 15px;
                flex: 1;
                min-width: 200px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }}
            .card-title {{
                font-size: 0.9em;
                margin-bottom: 10px;
                color: #666;
            }}
            .card-value {{
                font-size: 1.8em;
                font-weight: bold;
                color: #0066cc;
            }}
            .month-selector {{
                display: flex;
                flex-wrap: wrap;
                gap: 10px;
                margin-bottom: 20px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Relatório de Custos AWS</h1>
            <div class="timestamp">Gerado em: {timestamp}</div>
            
            <div class="summary">
                <h2>Resumo dos Últimos 3 Meses</h2>
                <p>Total de Contas Analisadas: {len(summary_df)}</p>
            </div>
            
            <div class="tabs">
                <div class="tab active" onclick="showTab('resumo')">Resumo</div>
                <div class="tab" onclick="showTab('detalhes-meses')">Detalhes por Mês</div>
                <div class="tab" onclick="showTab('todos-meses')">Todos os Meses</div>
            </div>
            
            <div id="resumo" class="tab-content active">
                <input type="text" id="searchResumo" class="search" placeholder="Buscar por conta..." onkeyup="filterTable('resumoTable', 'searchResumo')">
                
                <div class="service-legend">
                    <h3>Serviços analisados:</h3>
                    <div style="display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px;">
                        {service_legend_html}
                    </div>
                </div>
                
                <div style="overflow-x: auto;">
                    <table id="resumoTable">
                        <thead>
                            <tr>
                                {summary_headers}
                            </tr>
                        </thead>
                        <tbody>
                            {summary_table_body}
                        </tbody>
                    </table>
                </div>
            </div>
            
            <div id="detalhes-meses" class="tab-content">
                <input type="text" id="searchDetalhesMeses" class="search" placeholder="Buscar por conta..." onkeyup="filterCurrentMonthTable(this.value)">
                
                <div class="month-tabs">
                    {month_tabs_html}
                </div>
                
                {month_contents_html}
            </div>
            
            <div id="todos-meses" class="tab-content">
                <input type="text" id="searchTodosMeses" class="search" placeholder="Buscar por conta ou mês..." onkeyup="filterTable('todosMesesTable', 'searchTodosMeses')">
                
                <div style="overflow-x: auto;">
                    <table id="todosMesesTable">
                        <thead>
                            <tr>
                                {all_monthly_headers}
                            </tr>
                        </thead>
                        <tbody>
                            {all_monthly_table_body}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        
        <script>
            // Variáveis globais para rastrear estado
            var activeMainTab = 'resumo';
            var activeMonthTab = '';
            
            // Inicializar primeira aba de mês como ativa
            document.addEventListener('DOMContentLoaded', function() {{
                // Encontrar a aba de mês mais recente e ativá-la
                var monthTabs = document.querySelectorAll('.month-tab');
                if (monthTabs.length > 0) {{
                    var firstMonthId = monthTabs[0].id.replace('tab-', '');
                    activeMonthTab = firstMonthId;
                }}
                
                // Destacar células com percentuais altos
                highlightHighPercentages();
            }});
            
            function showTab(tabId) {{
                // Ocultar todos os conteúdos
                var contents = document.getElementsByClassName('tab-content');
                for (var i = 0; i < contents.length; i++) {{
                    contents[i].classList.remove('active');
                }}
                
                // Atualizar abas principais
                var tabs = document.getElementsByClassName('tab');
                for (var i = 0; i < tabs.length; i++) {{
                    tabs[i].classList.remove('active');
                }}
                
                // Mostrar conteúdo selecionado
                document.getElementById(tabId).classList.add('active');
                
                // Atualizar aba ativa
                event.currentTarget.classList.add('active');
                
                // Atualizar estado global
                activeMainTab = tabId;
            }}
            
            function showMonthTab(monthId) {{
                // Ocultar todos os conteúdos de mês
                var monthContents = document.getElementsByClassName('month-content');
                for (var i = 0; i < monthContents.length; i++) {{
                    monthContents[i].style.display = 'none';
                }}
                
                // Atualizar abas de mês
                var monthTabs = document.getElementsByClassName('month-tab');
                for (var i = 0; i < monthTabs.length; i++) {{
                    monthTabs[i].classList.remove('active');
                }}
                
                // Mostrar conteúdo do mês selecionado
                document.getElementById('month-' + monthId).style.display = 'block';
                
                // Atualizar aba ativa
                document.getElementById('tab-' + monthId).classList.add('active');
                
                // Atualizar estado global
                activeMonthTab = monthId;
            }}
            
            function filterTable(tableId, inputId) {{
                var input, filter, table, tr, td, i, j, txtValue, found;
                input = document.getElementById(inputId);
                filter = input.value.toUpperCase();
                table = document.getElementById(tableId);
                tr = table.getElementsByTagName("tr");
                
                for (i = 1; i < tr.length; i++) {{
                    found = false;
                    td = tr[i].getElementsByTagName("td");
                    
                    for (j = 0; j < 3; j++) {{
                        if (td[j]) {{
                            txtValue = td[j].textContent || td[j].innerText;
                            if (txtValue.toUpperCase().indexOf(filter) > -1) {{
                                found = true;
                                break;
                            }}
                        }}
                    }}
                    
                    if (found) {{
                        tr[i].style.display = "";
                    }} else {{
                        tr[i].style.display = "none";
                    }}
                }}
            }}
            
            function filterCurrentMonthTable(filterText) {{
                if (!activeMonthTab) return;
                
                var filter = filterText.toUpperCase();
                var table = document.getElementById('table-' + activeMonthTab);
                var tr = table.getElementsByTagName("tr");
                
                for (var i = 1; i < tr.length; i++) {{
                    var found = false;
                    var td = tr[i].getElementsByTagName("td");
                    
                    for (var j = 0; j < 2; j++) {{
                        if (td[j]) {{
                            var txtValue = td[j].textContent || td[j].innerText;
                            if (txtValue.toUpperCase().indexOf(filter) > -1) {{
                                found = true;
                                break;
                            }}
                        }}
                    }}
                    
                    if (found) {{
                        tr[i].style.display = "";
                    }} else {{
                        tr[i].style.display = "none";
                    }}
                }}
            }}
            
            function highlightHighPercentages() {{
                const tables = document.querySelectorAll('table');
                tables.forEach(table => {{
                    const headerRow = table.querySelector('tr');
                    if (!headerRow) return;
                    
                    const headers = headerRow.querySelectorAll('th');
                    
                    for (let i = 0; i < headers.length; i++) {{
                        if (headers[i].textContent.includes('% CloudWatch') || 
                            headers[i].textContent.includes('% Config')) {{
                            const colIndex = i;
                            
                            const rows = table.querySelectorAll('tbody tr');
                            for (let j = 0; j < rows.length; j++) {{
                                const cell = rows[j].querySelectorAll('td')[colIndex];
                                if (cell) {{
                                    const percentText = cell.textContent.trim();
                                    const percentValue = parseFloat(percentText);
                                    if (!isNaN(percentValue) && percentValue > 10) {{
                                        cell.classList.add('high-percentage');
                                    }}
                                }}
                            }}
                        }}
                    }}
                }});
            }}
        </script>
    </body>
    </html>"""
        
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
                        help='Lista de nomes de funções preferidas, em ordem de prioridade')
    parser.add_argument('--format', choices=['excel', 'html', 'both'], default='both', 
                        help='Formato do relatório: excel, html, ou both (ambos)')
    parser.add_argument('--output-excel', type=str, default='aws_cost_report.xlsx',
                        help='Caminho para salvar o relatório Excel')
    parser.add_argument('--output-html', type=str, default='aws_cost_report.html',
                        help='Caminho para salvar o relatório HTML')
    parser.add_argument('--account-filter', type=str,
                        help='Filtrar contas por nome ou ID (aceita expressões regulares)')
    parser.add_argument('--max-accounts', type=int,
                        help='Número máximo de contas a processar (útil para testes)')
    
    args = parser.parse_args()
    
    # Inicializar autenticador SSO
    sso_auth = AWSSSOAuth(
        start_url=args.start_url,
        region=args.region,
        sso_region=args.sso_region
    )
    
    # Autenticar e obter token
    print("Iniciando autenticação AWS SSO...")
    access_token = sso_auth.authenticate()
    
    if not access_token:
        print("Falha na autenticação. Encerrando.")
        sys.exit(1)
    
    # Inicializar extrator de custos
    extractor = AWSCostExtractor(sso_auth, access_token)
    
    # Extrair custos de todas as contas
    print("Iniciando extração de custos da AWS...")
    extractor.extract_all_accounts_cost(
        account_filter=args.account_filter,
        max_accounts=args.max_accounts,
        role_name_preference=args.preferred_roles
    )
    
    # Gerar relatórios conforme solicitado
    if args.format in ['excel', 'both']:
        extractor.generate_excel_report(output_path=args.output_excel)
    
    if args.format in ['html', 'both']:
        extractor.generate_html_report(output_path=args.output_html)
    
    print("Processo concluído!")


if __name__ == "__main__":
    main()

