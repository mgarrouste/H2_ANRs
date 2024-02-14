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
#SCF_TO_KGH2 = 0.002408 #kgh2/scf
NAT_GAS_PRICE = 6.45 #$/MMBTU
CONV_MJ_TO_MMBTU = 1/1055.05585 #MMBTU/MJ
EFF_H2_SMR = 159.6 #MJ/kgH2

WACC = utils.WACC


def get_refinery_demand(ref_id):
  ref_df = pd.read_excel('h2_demand_refineries.xlsx', sheet_name='processed')
  select_df = ref_df[ref_df['refinery_id']==ref_id]
  demand_kg_day = select_df['Corrected 2022 demand (kg/day)'].iloc[0]
  return demand_kg_day

def get_state(ref_id):
  ref_df = pd.read_excel('h2_demand_refineries.xlsx', sheet_name='processed')
  select_df = ref_df[ref_df['refinery_id']==ref_id]
  state= select_df['State'].iloc[0]
  return state


def solve_refinery_deployment(ref_id, ANR_data, H2_data):
  print(f'Start solve for {ref_id}')
  model = ConcreteModel(ref_id)

  #### Data ####
  demand_daily = get_refinery_demand(ref_id)
  model.pRefDem = Param(initialize=demand_daily) # kg/day
  state = get_state(ref_id)
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
  # Financial
  model.pWACC = Param(initialize = WACC)
  model.pITC_ANR = Param(initialize = utils.ITC_ANR)
  model.pITC_H2 = Param(initialize = utils.ITC_H2)

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

  def annualized_costs(model):
    costs =  sum(sum(model.pANRCap[g]*model.vM[n,g]*((model.pANRCAPEX[g]*(1-model.pITC_ANR)*model.pANRCRF[g]+model.pANRFC[g])+model.pANRVOM[g]*365*24) \
      + sum(model.pH2CapElec[h,g]*model.vQ[n,h,g]*(model.pH2CAPEX[h]*(1-model.pITC_H2)*model.pH2CRF[h]+model.pH2FC[h]+model.pH2VOM[h]*365*24) for h in model.H) for g in model.G) for n in model.N) 
    return costs

  def annualized_net_rev(model):
    return -annualized_costs(model)
  model.NetRevenues = Objective(expr=annualized_net_rev, sense=maximize)  


  #### Constraints ####
  # Meet refinery demand
  model.meet_ref_demand = Constraint(
    expr = model.pRefDem <= sum(sum(sum(model.vQ[n,h,g]*model.pH2CapH2[h]*24 for g in model.G) for h in model.H)for n in model.N)
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
    return sum(sum(model.pANRCap[g]*model.vM[n,g]*model.pANRCAPEX[g]*model.pANRCRF[g]for g in model.G) for n in model.N) 
  
  def compute_anr_om(model):
    return sum(sum(model.pANRCap[g]*model.vM[n,g]*(model.pANRFC[g]+model.pANRVOM[g]*365*24) for g in model.G) for n in model.N) 
  
  def compute_h2_capex(model):
    return sum(sum(sum(model.pH2CapElec[h,g]*model.vQ[n,h,g]*model.pH2CAPEX[h]*model.pH2CRF[h] for h in model.H) for g in model.G) for n in model.N) 
  
  def compute_h2_om(model):
    return sum(sum(sum(model.pH2CapElec[h,g]*model.vQ[n,h,g]*(model.pH2FC[h]+model.pH2VOM[h]*365*24) for h in model.H) for g in model.G) for n in model.N) 

  def get_crf(model):
    return sum(model.vS[g]*model.pANRCRF[g] for g in model.G)
  
  def get_deployed_cap(model):
    return sum(sum (model.vM[n,g]*model.pANRCap[g] for g in model.G) for n in model.N)

  #### SOLVE with CPLEX ####
  opt = SolverFactory('cplex')

  results = opt.solve(model, tee = False)
  results_ref = {}
  results_ref['ref_id'] = ref_id
  results_ref['state'] = value(model.pState)
  results_ref['Ref. Dem. (kg/day)'] = value(model.pRefDem)
  results_ref['Net Revenues ($/year)'] = value(model.NetRevenues)
  results_ref['Ann. carbon emissions (kgCO2eq/year)'] = value(compute_annual_carbon_emissions(model))
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
    print(f'Refning plant {ref_id} solved')
    return results_ref
  else:
    print('Not feasible.')
    return None


def compute_breakeven_price(results_ref):
  revenues = results_ref['Net Revenues ($/year)']
  breakeven_price = -revenues/(EFF_H2_SMR * CONV_MJ_TO_MMBTU * results_ref['Ref. Dem. (kg/day)']*365)
  return breakeven_price


def main(learning_rate_anr_capex=0, learning_rate_h2_capex=0, wacc=WACC, print_main_results=True):
  abspath = os.path.abspath(__file__)
  dname = os.path.dirname(abspath)
  os.chdir(dname)
  ref_df = pd.read_excel('h2_demand_refineries.xlsx', sheet_name='processed')
  ref_ids = list(ref_df['refinery_id'])

  ANR_data, H2_data = utils.load_data(learning_rate_anr_capex, learning_rate_h2_capex)

  breakeven_df = pd.DataFrame(columns=['ref_id', 'state', 'Ref. Dem. (kg/day)','Alkaline', 'HTSE', 'PEM', 'ANR type', \
                                       '# ANR modules','Breakeven price ($/MMBtu)', 'Ann. carbon emissions (kgCO2eq/year)'])

  with Pool(10) as pool: 
    results = pool.starmap(solve_refinery_deployment, [(ref_id, ANR_data, H2_data) for ref_id in ref_ids])
  pool.close()

  breakeven_df = pd.DataFrame(results)


  if print_main_results:
    breakeven_df.sort_values(by=['Breakeven price ($/MMBtu)'], inplace=True)
    csv_path = './results/refining_anr_lr_'+str(learning_rate_anr_capex)+'_h2_lr_'+str(learning_rate_h2_capex)+'_wacc_'+str(wacc)+'.csv'
    breakeven_df.to_csv(csv_path, header = True, index=False)

  # Median Breakeven price
  med_be = breakeven_df['Breakeven price ($/MMBtu)'].median()
  return med_be


if __name__ == '__main__':
  main()