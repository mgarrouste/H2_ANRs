from pyomo.environ import *
import pandas as pd
import numpy as np
import csv, os
import utils
from multiprocessing import Pool

"""version 0.2 Relaxed the heat balance constraint to be <= instead of ==, now the problem is feasible
  version 0.3, restructure code to save results of refinery deployment to csv file, 
  version 0.4 Solving with CPLEX, cannot solve problems with non-convex constraints so removing the wasteHeat variable
  version 1.0 Add revenues to the optimization function from avoided natural gas purchases
  version 2.0 Computation of breakeven natural gas price for each refinery and optimal ANR-H2 configuration
  version 2.1 Compute annual CO2 emissions from carbon intensity of nuclear energy"""

MaxANRMod = 20
WACC = utils.WACC
#SCF_TO_KGH2 = 0.002408 #kgh2/scf
NAT_GAS_PRICE = 6.45 #$/MMBTU
CONV_MJ_TO_MMBTU = 1/1055.05585 #MMBTU/MJ
CONV_MWh_to_MJ = 3600 #MJ/MWh
# GF: Glass Furnace
GFCAPEX = 1340000 #$/MWth
GFLT = 12 # years

def get_state(plant_id):
  ref_df = pd.read_excel('h2_demand_industry_heat.xlsx', sheet_name='max')
  select_df = ref_df[ref_df['FACILITY_ID']==plant_id]
  state = select_df['STATE'].iloc[0]
  return state

def get_plant_demand(plant_id):
  ref_df = pd.read_excel('h2_demand_industry_heat.xlsx', sheet_name='max')
  select_df = ref_df[ref_df['FACILITY_ID']==plant_id]
  demand_kg_day = select_df['H2 demand (kg/year)'].iloc[0]/365
  return demand_kg_day

def get_heat_demand(plant_id):
  ref_df = pd.read_excel('h2_demand_industry_heat.xlsx', sheet_name='max')
  select_df = ref_df[ref_df['FACILITY_ID']==plant_id]
  yearly_heat_demand = select_df['Heat demand (MJ/year)'].iloc[0]
  return yearly_heat_demand

def solve_process_heat_deployment(plant_id, ANR_data, H2_data):
  print(f'Start solve for {plant_id}')
  model = ConcreteModel(plant_id)

  #### Data ####
  demand_daily = get_plant_demand(plant_id)
  model.pH2Dem = Param(initialize=demand_daily) # kg/day
  yearly_heat_demand = get_heat_demand(plant_id)
  model.pHeatDem = Param(initialize = yearly_heat_demand) #MJ/year
  state = get_state(plant_id)
  model.pState = Param(initialize = state, within=Any)


  #### Sets ####
  model.N = Set(initialize=list(range(MaxANRMod)))
  model.H = Set(initialize=list(set([elem[0] for elem in H2_data.index])))
  model.G = Set(initialize=ANR_data.index)


  #### Variables ####
  model.vS = Var(model.G, within=Binary, doc='Chosen ANR type')
  model.vM = Var(model.N, model.G, within=Binary, doc='Indicator of built ANR module')
  model.vQ = Var(model.N, model.H, model.G, within=NonNegativeIntegers, doc='Nb of H2 module of type H for an ANR module of type g')

  #### Parameters ####
  model.pWACC = Param(initialize = WACC)
  model.pITC_H2 = Param(initialize = utils.ITC_H2)
  model.pITC_ANR = Param(initialize = utils.ITC_ANR)
  

  ### Glass furnace fueled by hydrogen ###
  model.pGFCAPEX = Param(initialize = GFCAPEX)#$/MWth
  model.pGFLT = Param(initialize = GFLT) # years

  ### H2 ###
  data = H2_data.reset_index(level='ANR')[['H2Cap (kgh2/h)']]
  data.drop_duplicates(inplace=True)
  model.pH2CapH2 = Param(model.H, initialize = data.to_dict()['H2Cap (kgh2/h)'])

  @model.Param(model.H, model.G)
  def pH2CapElec(model, h, g):
    return float(H2_data.loc[h,g]['H2Cap (MWe)'])

  # Electric and heat consumption
  @model.Param(model.H, model.G)
  def pH2ElecCons(model, h, g):
    return float(H2_data.loc[h,g]['H2ElecCons (MWhe/kgh2)'])

  @model.Param(model.H, model.G)
  def pH2HeatCons(model, h, g):
    return float(H2_data.loc[h,g]['H2HeatCons (MWht/kgh2)'])

  data = H2_data.reset_index(level='ANR')[['VOM ($/MWhe)']]
  data.drop_duplicates(inplace=True)
  model.pH2VOM = Param(model.H, initialize = data.to_dict()['VOM ($/MWhe)'])

  data = H2_data.reset_index(level='ANR')[['FOM ($/MWe-year)']]
  data.drop_duplicates(inplace=True)
  model.pH2FC = Param(model.H, initialize = data.to_dict()['FOM ($/MWe-year)'])

  data = H2_data.reset_index(level='ANR')[['CAPEX ($/MWe)']]
  data.drop_duplicates(inplace=True)
  model.pH2CAPEX = Param(model.H, initialize = data.to_dict()['CAPEX ($/MWe)'])

  @model.Param(model.H)
  def pH2CRF(model, h):
    data = H2_data.reset_index(level='ANR')[['Life (y)']]
    data = data.groupby(level=0).mean()
    crf = model.pWACC / (1 - (1/(1+model.pWACC)**float(data.loc[h,'Life (y)']) ) )
    return crf

  @model.Param(model.H, model.G)
  def pH2CarbonInt(model, h, g):
    return float(H2_data.loc[h,g]['Carbon intensity (kgCO2eq/kgH2)'])

  ### ANR ###
  # Capacity of ANRs MWt
  @model.Param(model.G)
  def pANRCap(model, g):
    return float(ANR_data.loc[g]['Power in MWe'])

  @model.Param(model.G)
  def pANRVOM(model, g):
    return float(ANR_data.loc[g]['VOM in $/MWh-e'])

  @model.Param(model.G)
  def pANRFC(model, g):
    return float(ANR_data.loc[g]['FOPEX $/MWe-y'])

  @model.Param(model.G)
  def pANRCAPEX(model, g):
    return float(ANR_data.loc[g]['CAPEX $/MWe'])

  @model.Param(model.G)
  def pANRCRF(model, g):
    return model.pWACC / (1 - (1/(1+model.pWACC)**float(ANR_data.loc[g,'Life (y)'])))
    
  @model.Param(model.G)
  def pANRThEff(model, g):
    return float(ANR_data.loc[g]['Power in MWe']/ANR_data.loc[g]['Power in MWt'])



  #### Objective ####  

  def annualized_costs_anr_h2(model):
    costs =  sum(sum(model.pANRCap[g]*model.vM[n,g]*((model.pANRCAPEX[g]*(1-model.pITC_ANR)*model.pANRCRF[g]+model.pANRFC[g])+model.pANRVOM[g]*365*24) \
      + sum(model.pH2CapElec[h,g]*model.vQ[n,h,g]*(model.pH2CAPEX[h]*(1-model.pITC_H2)*model.pH2CRF[h]+model.pH2FC[h]+model.pH2VOM[h]*365*24) for h in model.H) for g in model.G) for n in model.N) 
    return costs
  
  def annualized_costs_gf(model):
    #capital recovery factor
    gf_crf = model.pWACC / (1 - (1/(1+model.pWACC)**model.pGFLT) ) 
    heat_power_dem = model.pHeatDem/(CONV_MWh_to_MJ*365*24) #MWth
    costs = heat_power_dem*model.pGFCAPEX*gf_crf*(1-model.pITC_H2)
    return costs

  def annualized_net_rev(model):
    return -annualized_costs_anr_h2(model) - annualized_costs_gf(model)
  model.NetRevenues = Objective(expr=annualized_net_rev, sense=maximize)  


  #### Constraints ####
  # Meet refinery demand
  model.meet_ref_demand = Constraint(
    expr = model.pH2Dem <= sum(sum(sum(model.vQ[n,h,g]*model.pH2CapH2[h]*24 for g in model.G) for h in model.H)for n in model.N)
  )

  # Only one type of ANR deployed 
  model.max_ANR_type = Constraint(expr = sum(model.vS[g] for g in model.G)<=1)

  # Only modules of the chosen ANR type can be built
  def match_ANR_type(model, n, g):
    return model.vM[n,g] <= model.vS[g]
  model.match_ANR_type = Constraint(model.N, model.G, rule=match_ANR_type)


  # Heat and electricity balance
  def heat_elec_balance(model, n, g):
    return sum(model.pH2CapElec[h,g]*model.vQ[n,h,g]/model.pANRThEff[g] for h in model.H) <= (model.pANRCap[g]/model.pANRThEff[g])*model.vM[n,g]
  model.heat_elec_balance = Constraint(model.N, model.G, rule=heat_elec_balance)


  #### DATA ####
  def compute_annual_carbon_emissions(model):
    return sum(sum(sum(model.pH2CarbonInt[h,g]*model.vQ[n,h,g]*model.pH2CapH2[h]*24*365 for g in model.G) for h in model.H) for n in model.N)
  
  def compute_anr_capex(model):
    return sum(sum(model.pANRCap[g]*model.vM[n,g]*model.pANRCAPEX[g]*(1-model.pITC_ANR)*model.pANRCRF[g]for g in model.G) for n in model.N) 
  
  def compute_anr_om(model):
    return sum(sum(model.pANRCap[g]*model.vM[n,g]*(model.pANRFC[g]+model.pANRVOM[g]*365*24) for g in model.G) for n in model.N) 
  
  def compute_h2_capex(model):
    return sum(sum(sum(model.pH2CapElec[h,g]*model.vQ[n,h,g]*model.pH2CAPEX[h]*(1-model.pITC_H2)*model.pH2CRF[h] for h in model.H) for g in model.G) for n in model.N) 
  
  def compute_h2_om(model):
    return sum(sum(sum(model.pH2CapElec[h,g]*model.vQ[n,h,g]*(model.pH2FC[h]+model.pH2VOM[h]*365*24) for h in model.H) for g in model.G) for n in model.N) 

  def get_crf(model):
    return sum(model.vS[g]*model.pANRCRF[g] for g in model.G)
  
  def compute_conv_costs(model):
    gf_crf = model.pWACC / (1 - (1/(1+model.pWACC)**model.pGFLT) ) 
    heat_power_dem = model.pHeatDem/(CONV_MWh_to_MJ*365*24) #MWth
    costs = heat_power_dem*model.pGFCAPEX*gf_crf*(1-model.pITC_H2)
    return costs
  
  def get_deployed_cap(model):
    return sum(sum (model.vM[n,g]*model.pANRCap[g] for g in model.G) for n in model.N)

  #### SOLVE with CPLEX ####
  opt = SolverFactory('cplex')

  results = opt.solve(model, tee = False)
  results_ref = {}
  results_ref['id'] = plant_id
  results_ref['state'] = value(model.pState)
  results_ref['H2 Dem. (kg/day)'] = value(model.pH2Dem)
  results_ref['Heat Dem. (MJ/year)'] = value(model.pHeatDem)
  results_ref['Net Revenues ($/year)'] = value(model.NetRevenues)
  for h in model.H:
    results_ref[h] = 0
  if results.solver.termination_condition == TerminationCondition.optimal: 
    model.solutions.load_from(results)
    results_ref['Ann. CO2 emissions (kgCO2eq/year)'] = value(compute_annual_carbon_emissions(model))
    results_ref['ANR CAPEX ($/year)'] = value(compute_anr_capex(model))
    results_ref['H2 CAPEX ($/year)'] = value(compute_h2_capex(model))
    results_ref['ANR O&M ($/year)'] = value(compute_anr_om(model))
    results_ref['H2 O&M ($/year)'] = value(compute_h2_om(model))
    results_ref['ANR CRF'] = value(get_crf(model))
    results_ref['Depl. ANR Cap. (MWe)'] = value(get_deployed_cap(model))
    for g in model.G: 
      if value(model.vS[g]) >=1: 
        results_ref['ANR type'] = g
        total_nb_modules = int(np.sum([value(model.vM[n,g]) for n in model.N]))
        results_ref['# ANR modules'] = total_nb_modules
        for n in model.N:
          if value(model.vM[n,g]) >=1:
            for h in model.H:
              results_ref[h] += value(model.vQ[n,h,g])
    results_ref['Breakeven price ($/MMBtu)'] = compute_breakeven_price(results_ref)
    print(f'Process heat for plant {plant_id} solved')
    return results_ref
  else:
    print('Not feasible.')
    return None

def compute_breakeven_price(results_ref):
  anr_h2_rev = results_ref['Net Revenues ($/year)']
  heat_demand = results_ref['Heat Dem. (MJ/year)']*CONV_MJ_TO_MMBTU
  breakeven_price = -anr_h2_rev/heat_demand
  return breakeven_price


def main(learning_rate_anr_capex =0, learning_rate_h2_capex=0, wacc=WACC, print_main_results=True):
  abspath = os.path.abspath(__file__)
  dname = os.path.dirname(abspath)
  os.chdir(dname)
  demand_df = pd.read_excel('h2_demand_industry_heat.xlsx', sheet_name='max')
  plant_ids = list(demand_df['FACILITY_ID'])
  ANR_data, H2_data = utils.load_data(learning_rate_anr_capex, learning_rate_h2_capex)

  with Pool() as pool: 
    results = pool.starmap(solve_process_heat_deployment, [(plant, ANR_data, H2_data) for plant in plant_ids])
  pool.close()

  df = pd.DataFrame(results)

  excel_file = './results/raw_results_anr_lr_'+str(learning_rate_anr_capex)+'_h2_lr_'+str(learning_rate_h2_capex)+'_wacc_'+str(wacc)+'.xlsx'
  sheet_name = 'process_heat'
  if print_main_results:
    # Try to read the existing Excel file
    
    try:
    # Load the existing Excel file
      with pd.ExcelFile(excel_file, engine='openpyxl') as xls:
          # Check if the sheet exists
          if sheet_name in xls.sheet_names:
              # If the sheet exists, replace the data
              with pd.ExcelWriter(excel_file, engine='openpyxl', mode='a', if_sheet_exists='replace') as writer:
                  df.to_excel(writer, sheet_name=sheet_name, index=False)
          else:
              # If the sheet doesn't exist, create a new sheet
              with pd.ExcelWriter(excel_file, engine='openpyxl', mode='a') as writer:
                  df.to_excel(writer, sheet_name=sheet_name, index=False)
    except FileNotFoundError:
        # If the file doesn't exist, create a new one and write the DataFrame to it
        df.to_excel(excel_file, sheet_name=sheet_name, index=False)
  
  
  # Return median breakeven price
  med_be = df['Breakeven price ($/MMBtu)'].median()
  return med_be


if __name__ == '__main__':
  main()