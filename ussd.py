"""
USSD Integration via Africa's Talking.

Set these environment variables:
  AT_USERNAME   - your Africa's Talking username (use 'sandbox' for testing)
  AT_API_KEY    - your Africa's Talking API key

Africa's Talking sends POST requests to your callback URL with:
  sessionId, serviceCode, phoneNumber, text (cumulative user input)

Your app responds with plain text:
  CON <menu text>   → session continues, shows menu to user
  END <final text>  → session ends

Test using the AT simulator at:
  https://developers.africastalking.com/simulator
"""
import os
from datetime import datetime
from flask import Blueprint, request
from loguru import logger
from models import db, User, SystemState, MpesaTopup, Transaction
from trust_graph import compute_draw_ceiling, TrustGraphError
from mpesa import stk_push, MpesaError

ussd_bp = Blueprint('ussd', __name__, url_prefix='/ussd')

# ── Language strings ──────────────────────────────────────────────────────────

STRINGS = {
    'en': {
        'welcome_back':  "Welcome back, {name}",
        'welcome':       "Welcome to SolidarityPool",
        'opt_balance':   "1. Check balance & draw ceiling",
        'opt_roundup':   "2. Simulate round-up",
        'opt_care':      "3. Request care funds",
        'opt_trust':     "4. My trust score",
        'opt_topup':     "5. Top up via M-Pesa",
        'opt_provider':  "6. Provider payment check",
        'opt_help':      "7. Help / FAQ",
        'opt_repay':     "8. Repay social credit",
        'opt_language':  "9. Change language",
        'opt_exit':      "0. Exit",
        'goodbye':       "Thank you for using SolidarityPool.",
        'balance_title': "Your SolidarityPool Balance",
        'sub_wallet':    "Sub-wallet",
        'draw_ceiling':  "Draw ceiling",
        'social_credit': "Social credit",
        'communal_pool': "Communal pool",
        'enter_pin':     "Enter your PIN to check balance:",
        'wrong_pin':     "Incorrect PIN. Please dial again.",
        'trust_title':   "Your Trust Profile",
        'trust_score':   "Trust score",
        'witness_acc':   "Witness accuracy",
        'roundup_mult':  "Round-up multiplier",
        'enter_amount':  "Enter purchase amount (KES):",
        'split_info':    "Split: {w}% wallet, {p}% pool, {f}% fee",
        'roundup_done':  "Round-up complete!",
        'wallet_credit': "Your wallet: +KES {amt}",
        'pool_credit':   "Pool credited: KES {amt}",
        'new_balance':   "New balance: KES {bal}",
        'invalid_amount':"Invalid amount. Please enter a number, e.g. 500",
        'reg_name':      "Enter your full name:",
        'reg_pin':       "Choose a 4–6 digit PIN:",
        'reg_referrer':  "Enter referrer phone (or 0 to skip):",
        'reg_success':   "Registration successful!\nWelcome, {name}.\nDial again to access your account.",
        'already_reg':   "You are already registered. Dial again to log in.",
        'blank_name':    "Name cannot be blank. Please try again.",
        'pin_4digits':   "PIN must be 4–6 digits. Dial again.",
        'care_ceiling':  "Your draw ceiling: KES {ceiling:.0f}\nThis is the max you can request from the pool.\nEnter amount needed (KES):",
        'care_exceed':   "Amount exceeds your ceiling.\nYour draw ceiling is KES {ceiling:.0f}.\nPlease enter a lower amount or build your trust score.",
        'care_provider': "Enter provider code (e.g. MULAGO001):",
        'care_bad_prov': "Invalid provider code '{code}'.\nTry: {examples}\nOr ask your clinic for their provider code.",
        'care_done':     "Care request submitted!\nFrom your wallet: KES {sub:.2f}\nFrom pool: KES {pool:.2f}\nRemaining ceiling: KES {ceil:.0f}\nRequest ID: {rid}",
        'repay_none':    "You have no outstanding social credit to repay.",
        'repay_intro':   "Repay Social Credit\nOutstanding: UGX {credit:,.0f}\nEnter your PIN to continue:",
        'repay_enter':   "Enter amount to repay (UGX)\nWallet balance: UGX {bal:,.0f}\nOutstanding debt: UGX {credit:,.0f}",
        'repay_insuff':  "Insufficient wallet balance.\nYour wallet: UGX {bal:,.0f}\nTop up first, then repay.",
        'repay_done':    "Repaid UGX {amt:,.0f}.\nRemaining debt: UGX {credit:,.0f}\nTrust score: {score:.4f}\n(was {old:.4f})",
        'no_mpesa':      "M-Pesa top-up is not available. Contact support.",
        'topup_enter':   "Enter top-up amount (KES):",
        'topup_sent':    "M-Pesa prompt sent to {phone}.\nAmount: KES {amt}\nApprove on your phone — your wallet\nwill be credited automatically.",
        'topup_fail':    "M-Pesa prompt failed. Please try again later.",
        'prov_enter':    "Enter your provider code:",
        'prov_bad':      "Invalid code '{code}'.\nTry: {examples}\nAsk clinic admin for the correct code.",
        'prov_none':     "{name}: no payment records found yet.",
        'prov_title':    "{name} — last {n} payments:",
        'help_menu':     "SolidarityPool Help\n1. What is SolidarityPool?\n2. How do round-ups work?\n3. How to request care funds?\n4. What is a trust score?\n5. What is a draw ceiling?\n0. Back to main menu",
        'help_1':        "SolidarityPool is a community mutual-aid fund.\nMembers save via micro round-ups and can access\ncare funds for medical emergencies.",
        'help_2':        "When you buy e.g. UGX 12,500, we round up\nto UGX 13,000 and save UGX 500.\n70% → your wallet  20% → community pool\n10% → platform fee.",
        'help_3':        "Dial *384# → option 3 (Request care funds).\nEnter amount, then your clinic's provider code\n(e.g. MULAGO001 — ask your clinic).\n3 community members will verify your request.",
        'help_4':        "Your trust score (0–1) measures reliability:\nrepaying social credit, accurate witness votes,\nnetwork connections, and regular contributions.",
        'help_5':        "Your draw ceiling is the max you can request\nfrom the pool. It grows as your trust score rises\nand the pool stays healthy.\nCheck it: main menu → option 1 (Balance).",
        'help_back':     "Dial *384# again to return to the main menu.",
        'help_invalid':  "Invalid choice. Dial *384# again for help.",
        'invalid_opt':   "Invalid option. Please dial again and choose 1–9 or 0 to exit.",
        'session_err':   "Session error. Please dial again.",
        'lang_menu':     "Choose your language:\n1. English\n2. Luganda\n3. Kiswahili\n0. Back",
        'lang_saved':    "Language set to English. Dial again.",
        'lang_invalid':  "Invalid choice. Dial again.",
        'unreg_first':   "Please register first.\nDial again and select 1.",
        'opt_witness':   "5. Witness tasks",
        'opt_communities':"6. My community",
        'opt_support':   "7. Support & help",
        'opt_more':      "9. More options",
        'witness_pin_prompt': "Enter PIN to view witness tasks:",
        'witness_none':  "No pending witness tasks. Check back later.",
        'witness_count': "You have {n} pending witness task(s). PIN to continue:",
        'witness_item':  "Request from {name}\nAmount: UGX {amount:,.0f}\n1. Approve\n2. Reject\n0. Skip",
        'witness_voted': "Vote recorded. Thank you for supporting your community!",
        'community_none':"You have no community yet.\n1. Browse communities\n0. Back",
        'community_info':"{name}\nPool: UGX {pool:,.0f} ({pct:.0f}% full)\nMembers: {members}\n1. Request leave\n0. Back",
        'community_join_prompt': "Type community name to join:",
        'community_join_ok': "Joined {name}! Save together with your community.",
        'community_join_fail': "Community '{name}' not found. Check the name and try again.",
        'leave_confirm':    "Request to leave {name}?\n1. Yes, request leave\n2. Cancel",
        'leave_requested':  "Leave request submitted.\nA community admin will review it.",
        'leave_already':    "Leave already requested.\nAwaiting admin review.",
        'leave_blocked_admin':   "Cannot leave — you are the sole admin.\nPromote another member first.",
        'leave_blocked_credit':  "Cannot leave — outstanding social credit.\nRepay first (option 8).",
        'leave_blocked_care':    "Cannot leave — active care request in progress.",
        'leave_blocked_fraud':   "Cannot leave — account under fraud review.",
        'leave_blocked_dispute': "Cannot leave — unresolved payment dispute.",
        'support_menu':  "Support & Help\n1. Open a support ticket\n2. My tickets\n3. FAQ\n0. Back",
        'support_new_prompt': "Describe your issue (your message will be sent to support):",
        'support_submitted': "Ticket #{id} submitted! Support team will reply soon.",
        'support_my_none':  "No support tickets found for your account.",
        'support_my_title': "Your recent tickets:",
        'more_menu':     "More Options\n1. Provider payment check\n2. M-Pesa top-up\n3. Change language\n0. Back",
        'more_menu_admin': "More Options\n1. Provider payment check\n2. M-Pesa top-up\n3. Change language\n4. Admin panel\n0. Back",
        'admin_not_auth':"Admin access not authorized for this account.",
        'admin_summary': "Platform Stats [{role}]\nCare pending: {care}\nFraud alerts: {fraud}\nProviders: {prov}\nSupport: {sup}\nDisputes: {dis}",
    },
    'lg': {
        'welcome_back':  "Tukusubirira nate, {name}",
        'welcome':       "Tukusubirira ku SolidarityPool",
        'opt_balance':   "1. Kebera ssente n'obukulu bw'okusaba",
        'opt_roundup':   "2. Gezesa okuzingula ssente",
        'opt_care':      "3. Saba ssente z'obujjanjabi",
        'opt_trust':     "4. Ddaala lyange ly'okukkirizibwa",
        'opt_topup':     "5. Yongera ssente nga M-Pesa",
        'opt_provider':  "6. Kebera payment ya clinic",
        'opt_help':      "7. Obuyambi",
        'opt_repay':     "8. Zza obulemu bwo",
        'opt_language':  "9. Kyusa olulimi",
        'opt_exit':      "0. Vaamu",
        'goodbye':       "Webale okozesa SolidarityPool.",
        'balance_title': "Ssente Zo ku SolidarityPool",
        'sub_wallet':    "Simu ya ssente",
        'draw_ceiling':  "Obukulu bw'okusaba",
        'social_credit': "Obulemu bwa ssente",
        'communal_pool': "Ekisumuluzo kya ssente",
        'enter_pin':     "Yingiza PIN yo okebere ssente:",
        'wrong_pin':     "PIN etali ya ntuufu. Yita nate.",
        'trust_title':   "Ddaala Lyo ly'Okukkirizibwa",
        'trust_score':   "Ddaala ly'okukkirizibwa",
        'witness_acc':   "Butuufu bw'okabiriza",
        'roundup_mult':  "Okuzingula okugattibwamu",
        'enter_amount':  "Yingiza omuwendo gw'ogula (KES):",
        'split_info':    "Gabano: {w}% simu, {p}% ekisumuluzo, {f}% musolo",
        'roundup_done':  "Okuzingula kwakwata!",
        'wallet_credit': "Simu yo: +KES {amt}",
        'pool_credit':   "Ekisumuluzo: KES {amt}",
        'new_balance':   "Omuwendo ogupya: KES {bal}",
        'invalid_amount':"Omuwendo gutali wa ntuufu. Yita nate.",
        'reg_name':      "Yingiza erinnya lyo lyonna:",
        'reg_pin':       "Londa PIN y'emiwendo ena okutuuka mukaaga:",
        'reg_referrer':  "Yingiza simu y'oyo yakuzannyisa (oba 0 okusula):",
        'reg_success':   "Okyusiddwa nayee!\nTukusubirira, {name}.\nYita nate okugera akaawunti yo.",
        'already_reg':   "Wakyusiddwa. Yita nate okuyingira.",
        'blank_name':    "Erinnya tikisibwe. Gezaako nate.",
        'pin_4digits':   "PIN erina emiwendo ena okutuuka mukaaga. Yita nate.",
        'care_ceiling':  "Obukulu bwo: KES {ceiling:.0f}\nOno we obukulu bw'okusaba.\nYingiza omuwendo ogwetaaga (KES):",
        'care_exceed':   "Omuwendo ousei obukulu bwo.\nObukulu bwo bwa KES {ceiling:.0f}.\nYingiza omuwendo omuto oba yongera ddaala lyo.",
        'care_provider': "Yingiza koodi ya clinic (eg. MULAGO001):",
        'care_bad_prov': "Koodi '{code}' etali ya ntuufu.\nGezaako: {examples}\nYita clinic yo akuwe koodi.",
        'care_done':     "Okusaba kwayingiziddwa!\nEva mu simu yo: KES {sub:.2f}\nEva mu ekisumuluzo: KES {pool:.2f}\nObukulu obunsigadde: KES {ceil:.0f}\nOmubare: {rid}",
        'repay_none':    "Tolina bulemu bwa ssente okuzzaako.",
        'repay_intro':   "Zza Obulemu Bwo\nObulemu: UGX {credit:,.0f}\nYingiza PIN yo okukomyawo:",
        'repay_enter':   "Yingiza omuwendo oguzzaako (UGX)\nSimu yo: UGX {bal:,.0f}\nObulemu: UGX {credit:,.0f}",
        'repay_insuff':  "Ssente muke mu simu yo.\nSimu yo: UGX {bal:,.0f}\nYongera ssente, olubaawo ozzeko.",
        'repay_done':    "Ozzedde UGX {amt:,.0f}.\nObulemu obunsigadde: UGX {credit:,.0f}\nDdaala: {score:.4f}\n(kyali {old:.4f})",
        'no_mpesa':      "M-Pesa teriwo kati. Buulira basaawo.",
        'topup_enter':   "Yingiza omuwendo oguyongera (KES):",
        'topup_sent':    "M-Pesa yatumibwa ku {phone}.\nOmuwendo: KES {amt}\nKiriza ku simu yo.",
        'topup_fail':    "M-Pesa yalemwa. Gezaako nate.",
        'prov_enter':    "Yingiza koodi yo ya clinic:",
        'prov_bad':      "Koodi '{code}' etali ya ntuufu.\nGezaako: {examples}\nBuulira ssaako wa clinic.",
        'prov_none':     "{name}: teriwo payment ekyasalibwa.",
        'prov_title':    "{name} — payment {n} z'oluvannyuma:",
        'help_menu':     "Obuyambi bwa SolidarityPool\n1. Kiki SolidarityPool?\n2. Okuzingula kuyita otya?\n3. Otya okusaba ssente z'obujjanjabi?\n4. Kiki ddaala ly'okukkirizibwa?\n5. Kiki obukulu bw'okusaba?\n0. Subira ekyama",
        'help_1':        "SolidarityPool kiseera ky'obuyambi bw'ekibiina.\nAbagattiddwa balfunye ssente nga bakozesa\nokuzingula ne basaba obujjanjabi.",
        'help_2':        "Bw'ogula UGX 12,500, tozingula okutuuka\nUGX 13,000 ne woleka UGX 500.\n70% simu yo · 20% ekibiina · 10% musolo.",
        'help_3':        "Yita *384# → 3 (Saba ssente z'obujjanjabi).\nYingiza omuwendo, olubaawo koodi ya clinic\n(eg. MULAGO001).\nAbantu 3 mu kibiina balikkiriziganya.",
        'help_4':        "Ddaala (0–1) eraga obutuufu bwo:\nOkuzzaako obulemu, okabiriza, okujjukanya,\nne ntuufu mu mudde.",
        'help_5':        "Obukulu bwo kye kimu kyokuba okusaba.\nKiyitamu ng'eddaala lyo likulabirira.\nKebera: ekyama → 1 (Ssente).",
        'help_back':     "Yita *384# nate okugalamuka ku kiyama.",
        'help_invalid':  "Londa etali ya ntuufu. Yita *384# okufuna obuyambi.",
        'invalid_opt':   "Londa etali ya ntuufu. Yita nate oloonde 1–9 oba 0.",
        'session_err':   "Obuzibu mu session. Yita nate.",
        'lang_menu':     "Londa olulimi:\n1. Olungereza\n2. Oluganda\n3. Kiswahili\n0. Subira",
        'lang_saved':    "Oluganda lwasettingulibwa. Yita nate.",
        'lang_invalid':  "Londa etali ya ntuufu. Yita nate.",
        'unreg_first':   "Kyusiddwa okusooka.\nYita nate ne ulonda 1.",
        'opt_witness':   "5. Emirimu gy'abakkirizaani",
        'opt_communities': "6. Ekibiina kyange",
        'opt_support':   "7. Obuyambi",
        'opt_more':      "9. Ebirala",
        'witness_pin_prompt': "Yingiza PIN yo okebera emirimu gy'abakkirizaani:",
        'witness_none':  "Tewali saba ya kukkiriziganya kati.",
        'witness_count': "Olina emirimu {n} gy'abakkirizaani. PIN okukomyawo:",
        'witness_item':  "Okusaba okuva {name}\nOmuwendo: UGX {amount:,.0f}\n1. Kiriza\n2. Gaana\n0. Ssula",
        'witness_voted': "Eddembe lyo lyateereddwayo. Webale okuyamba ekibiina!",
        'community_none':"Tolina kibiina kati.\n1. Laba ebibiina\n0. Subira",
        'community_info':"{name}\nEkisumuluzo: UGX {pool:,.0f} ({pct:.0f}%)\nAbagattiddwa: {members}\n1. Saba okuva\n0. Subira",
        'community_join_prompt': "Wandiika erinnya ly'ekibiina ky'oyagala kuyingira:",
        'community_join_ok': "Wayingira {name}! Teeka ssente n'ekibiina kyo.",
        'community_join_fail': "Ekibiina '{name}' tekizuulwa. Kebera erinnya n'ogezeeko.",
        'leave_confirm':    "Saba okuva mu {name}?\n1. Yee, saba okuva\n2. Ssuula",
        'leave_requested':  "Okusaba okuvamu kwatumibwa.\nMuteeka-muteeka wa kibiina alabiridde.",
        'leave_already':    "Wasaba okuva dda.\nWeetaagisa okukkirizibwa.",
        'leave_blocked_admin':   "Toyinza kuva — oli ssaawo wo yekka.\nSimula omuntu omulala okusooka.",
        'leave_blocked_credit':  "Toyinza kuva — olina obulemu.\nZza okusooka (londa 8).",
        'leave_blocked_care':    "Toyinza kuva — olina okusaba kwa bujjanjabi okutali kutuukiridde.",
        'leave_blocked_fraud':   "Toyinza kuva — akaawunti yo eri mu nsaka ya ebibogo.",
        'leave_blocked_dispute': "Toyinza kuva — olina obutatuukiridde bw'okuwa.",
        'support_menu':  "Obuyambi\n1. Saba obuyambi\n2. Okusaba kwange\n3. Ebibuuzo\n0. Subira",
        'support_new_prompt': "Wandiika obuzibu bwo (butumibwe ku ssaawo):",
        'support_submitted': "Okusaba #{id} kwatumibwa! Bazaamu mangu.",
        'support_my_none':  "Tosaba bwa buyambi.",
        'support_my_title': "Okusaba kwako okuggya:",
        'more_menu':     "Ebirala\n1. Kebera payment ya clinic\n2. M-Pesa\n3. Kyusa olulimi\n0. Subira",
        'more_menu_admin': "Ebirala\n1. Kebera payment\n2. M-Pesa\n3. Kyusa olulimi\n4. Ssaawo panel\n0. Subira",
        'admin_not_auth':"Obutaali kw'okuyingira ssaawo.",
        'admin_summary': "Mawulire ga Pletifoomu [{role}]\nOkusaba: {care}\nEbibogo: {fraud}\nAbapedde: {prov}\nObuyambi: {sup}\nObutatuukiridde: {dis}",
    },
    'sw': {
        'welcome_back':  "Karibu tena, {name}",
        'welcome':       "Karibu SolidarityPool",
        'opt_balance':   "1. Angalia salio na kikomo cha ombi",
        'opt_roundup':   "2. Simula round-up",
        'opt_care':      "3. Omba fedha za matibabu",
        'opt_trust':     "4. Alama yangu ya imani",
        'opt_topup':     "5. Weka salio via M-Pesa",
        'opt_provider':  "6. Kagua malipo ya kliniki",
        'opt_help':      "7. Msaada / Maswali",
        'opt_repay':     "8. Lipa deni lako",
        'opt_language':  "9. Badilisha lugha",
        'opt_exit':      "0. Toka",
        'goodbye':       "Asante kwa kutumia SolidarityPool.",
        'balance_title': "Salio Lako la SolidarityPool",
        'sub_wallet':    "Mkoba wako",
        'draw_ceiling':  "Kikomo cha ombi",
        'social_credit': "Deni la kijamii",
        'communal_pool': "Mfuko wa jamii",
        'enter_pin':     "Ingiza PIN yako kuangalia salio:",
        'wrong_pin':     "PIN si sahihi. Piga tena.",
        'trust_title':   "Wasifu Wako wa Imani",
        'trust_score':   "Alama ya imani",
        'witness_acc':   "Usahihi wa ushuhuda",
        'roundup_mult':  "Kizidishio cha round-up",
        'enter_amount':  "Ingiza kiasi cha manunuzi (KES):",
        'split_info':    "Mgawanyo: {w}% mkoba, {p}% mfuko, {f}% ada",
        'roundup_done':  "Round-up imekamilika!",
        'wallet_credit': "Mkoba wako: +KES {amt}",
        'pool_credit':   "Mfuko uliongezwa: KES {amt}",
        'new_balance':   "Salio jipya: KES {bal}",
        'invalid_amount':"Kiasi si sahihi. Ingiza nambari, mfano 500",
        'reg_name':      "Ingiza jina lako kamili:",
        'reg_pin':       "Chagua PIN ya nambari 4–6:",
        'reg_referrer':  "Ingiza nambari ya mkurugenzi (au 0 kuruka):",
        'reg_success':   "Usajili umefanikiwa!\nKaribu, {name}.\nPiga tena kuingia akaunti yako.",
        'already_reg':   "Umesajiliwa tayari. Piga tena kuingia.",
        'blank_name':    "Jina haliwezi kuwa tupu. Jaribu tena.",
        'pin_4digits':   "PIN lazima iwe nambari 4 hadi 6. Piga tena.",
        'care_ceiling':  "Kikomo chako: KES {ceiling:.0f}\nHiki ndicho kiasi unachoweza kuomba.\nIngiza kiasi unachohitaji (KES):",
        'care_exceed':   "Kiasi kinazidi kikomo chako.\nKikomo chako ni KES {ceiling:.0f}.\nIngiza kiasi kidogo zaidi au ongeza alama yako.",
        'care_provider': "Ingiza nambari ya kliniki (mfano MULAGO001):",
        'care_bad_prov': "Nambari '{code}' si sahihi.\nJaribu: {examples}\nAu uliza kliniki yako nambari yake.",
        'care_done':     "Ombi limewasilishwa!\nKutoka mkononi mwako: KES {sub:.2f}\nKutoka mfukoni: KES {pool:.2f}\nKikomo kilichobaki: KES {ceil:.0f}\nNambari ya ombi: {rid}",
        'repay_none':    "Huna deni la kulipa.",
        'repay_intro':   "Lipa Deni Lako\nDeni: UGX {credit:,.0f}\nIngiza PIN yako kuendelea:",
        'repay_enter':   "Ingiza kiasi cha kulipa (UGX)\nSalio lako: UGX {bal:,.0f}\nDeni: UGX {credit:,.0f}",
        'repay_insuff':  "Salio halitooshi.\nSalio lako: UGX {bal:,.0f}\nWeka salio kwanza, kisha lipa.",
        'repay_done':    "Umelipa UGX {amt:,.0f}.\nDeni lililobaki: UGX {credit:,.0f}\nAlama ya imani: {score:.4f}\n(ilikuwa {old:.4f})",
        'no_mpesa':      "M-Pesa haipatikani sasa. Wasiliana na msaada.",
        'topup_enter':   "Ingiza kiasi cha kuweka (KES):",
        'topup_sent':    "Ombi la M-Pesa limetumwa kwa {phone}.\nKiasi: KES {amt}\nKubali kwenye simu yako.",
        'topup_fail':    "M-Pesa imeshindwa. Jaribu tena baadaye.",
        'prov_enter':    "Ingiza nambari yako ya kliniki:",
        'prov_bad':      "Nambari '{code}' si sahihi.\nJaribu: {examples}\nUliza msimamizi wa kliniki.",
        'prov_none':     "{name}: hakuna rekodi za malipo bado.",
        'prov_title':    "{name} — malipo {n} ya hivi karibuni:",
        'help_menu':     "Msaada wa SolidarityPool\n1. SolidarityPool ni nini?\n2. Round-up inafanyaje kazi?\n3. Vipi kuomba fedha za matibabu?\n4. Alama ya imani ni nini?\n5. Kikomo cha ombi ni nini?\n0. Rudi nyuma",
        'help_1':        "SolidarityPool ni mfuko wa msaada wa jamii.\nWanachama wanaokoa kwa round-up ndogo\nna wanaweza kupata fedha za dharura za matibabu.",
        'help_2':        "Ukinunua UGX 12,500, tunazungusha hadi\nUGX 13,000 na kuokoa UGX 500.\n70% → mkoba wako · 20% → mfuko · 10% → ada.",
        'help_3':        "Piga *384# → chaguo 3 (Omba fedha).\nIngiza kiasi, kisha nambari ya kliniki\n(mfano MULAGO001 — uliza kliniki yako).\nWanachama 3 watathibitisha ombi lako.",
        'help_4':        "Alama yako ya imani (0–1) inapima:\nulipaji wa deni, ushuhuda sahihi,\nmtandao wa marafiki, na mchango wa mara kwa mara.",
        'help_5':        "Kikomo chako ni kiasi unachoweza kuomba.\nKinaongezeka alama yako inapopanda\nna mfuko ukiwa mzima.\nAngalia: menyu kuu → chaguo 1 (Salio).",
        'help_back':     "Piga *384# tena kurudi menyu kuu.",
        'help_invalid':  "Chaguo si sahihi. Piga *384# tena kwa msaada.",
        'invalid_opt':   "Chaguo si sahihi. Piga tena na chagua 1–9 au 0.",
        'session_err':   "Hitilafu ya kikao. Piga tena.",
        'lang_menu':     "Chagua lugha yako:\n1. Kiingereza\n2. Luganda\n3. Kiswahili\n0. Rudi",
        'lang_saved':    "Kiswahili kimewekwa. Piga tena.",
        'lang_invalid':  "Chaguo si sahihi. Piga tena.",
        'unreg_first':   "Tafadhali jisajili kwanza.\nPiga tena na chagua 1.",
        'opt_witness':   "5. Kazi za ushuhuda",
        'opt_communities': "6. Jamii yangu",
        'opt_support':   "7. Msaada & FAQ",
        'opt_more':      "9. Zaidi",
        'witness_pin_prompt': "Ingiza PIN kuona kazi za ushuhuda:",
        'witness_none':  "Hakuna maombi ya ushuhuda kwa sasa.",
        'witness_count': "Una kazi {n} za ushuhuda. PIN kuendelea:",
        'witness_item':  "Ombi kutoka {name}\nKiasi: UGX {amount:,.0f}\n1. Kubali\n2. Kataa\n0. Ruka",
        'witness_voted': "Kura imerekodiwa. Asante kwa kusaidia jamii!",
        'community_none':"Huna jamii bado.\n1. Tazama jamii\n0. Rudi",
        'community_info':"{name}\nMfuko: UGX {pool:,.0f} ({pct:.0f}%)\nWanachama: {members}\n1. Omba kuondoka\n0. Rudi",
        'community_join_prompt': "Andika jina la jamii unayotaka kujiunga:",
        'community_join_ok': "Umejiunga na {name}! Okoa pamoja.",
        'community_join_fail': "Jamii '{name}' haipatikani. Angalia jina na jaribu tena.",
        'leave_confirm':    "Omba kuondoka {name}?\n1. Ndiyo, omba kuondoka\n2. Ghairi",
        'leave_requested':  "Ombi la kuondoka limewasilishwa.\nMsimamizi wa jamii ataangalia.",
        'leave_already':    "Umekwisha omba kuondoka.\nSubiri idhini.",
        'leave_blocked_admin':   "Huwezi kuondoka — wewe ni msimamizi pekee.\nTeua mwingine kwanza.",
        'leave_blocked_credit':  "Huwezi kuondoka — una deni.\nLipa kwanza (chaguo 8).",
        'leave_blocked_care':    "Huwezi kuondoka — una ombi la matibabu inayoendelea.",
        'leave_blocked_fraud':   "Huwezi kuondoka — akaunti yako iko chini ya uchunguzi.",
        'leave_blocked_dispute': "Huwezi kuondoka — una mgogoro ambao haujatatuliwa.",
        'support_menu':  "Msaada & FAQ\n1. Fungua tiketi\n2. Tiketi zangu\n3. Maswali\n0. Rudi",
        'support_new_prompt': "Elezea tatizo lako (ujumbe utumwa kwa timu):",
        'support_submitted': "Tiketi #{id} imewasilishwa! Timu itajibu hivi karibuni.",
        'support_my_none':  "Huna tiketi za msaada.",
        'support_my_title': "Tiketi zako za hivi karibuni:",
        'more_menu':     "Zaidi\n1. Angalia malipo ya kliniki\n2. M-Pesa\n3. Badilisha lugha\n0. Rudi",
        'more_menu_admin': "Zaidi\n1. Angalia malipo\n2. M-Pesa\n3. Badilisha lugha\n4. Admin panel\n0. Rudi",
        'admin_not_auth':"Huna ruhusa ya admin.",
        'admin_summary': "Admin [{role}]\nMatibabu: {care}\nUlaghai: {fraud}\nWahudumu: {prov}\nMsaada: {sup}\nMigogoro: {dis}",
    },
}

# Phone prefix → default language mapping
_PREFIX_LANG = {
    '256': 'lg',   # Uganda → Luganda
    '254': 'sw',   # Kenya  → Swahili
    '255': 'sw',   # Tanzania → Swahili
    '250': 'sw',   # Rwanda → Swahili
}


def _get_lang(user=None, phone: str = '') -> str:
    """Return language code for this user/phone (en/lg/sw)."""
    if user and hasattr(user, 'preferred_language') and user.preferred_language:
        return user.preferred_language
    norm = phone.strip().lstrip('+')
    for prefix, lang in _PREFIX_LANG.items():
        if norm.startswith(prefix):
            return lang
    return 'en'


def t(key: str, lang: str = 'en', **kwargs) -> str:
    """Translate a string key, falling back to English."""
    d = STRINGS.get(lang, STRINGS['en'])
    tmpl = d.get(key, STRINGS['en'].get(key, key))
    if kwargs:
        try:
            return tmpl.format(**kwargs)
        except (KeyError, ValueError):
            return tmpl
    return tmpl


# ── helpers ──────────────────────────────────────────────────────────────────

def _normalize_phone(phone: str) -> str:
    """Strip leading + or spaces; Africa's Talking sends e.g. +254712345678."""
    return phone.strip().lstrip('+')


def _get_or_none(phone: str):
    return User.query.filter_by(phone=_normalize_phone(phone)).first()


# ── main callback ─────────────────────────────────────────────────────────────

@ussd_bp.route('/callback', methods=['POST'])
def callback():
    session_id = request.form.get('sessionId', '')
    phone = request.form.get('phoneNumber', '')
    text = request.form.get('text', '')

    logger.info("USSD session={} phone={} text={!r}", session_id, phone, text)

    steps = text.split('*') if text else ['']
    level = len(steps)
    response = _route(phone, steps, level)

    logger.info("USSD response → {!r}", response[:80])
    return response, 200, {'Content-Type': 'text/plain'}


# ── menu router ───────────────────────────────────────────────────────────────

def _route(phone: str, steps: list, level: int) -> str:
    user = _get_or_none(phone)
    lang = _get_lang(user, phone)

    # ── Level 0: main menu ───────────────────────────────────────────────────
    if steps[0] == '':
        if user:
            repay_line = t('opt_repay', lang) + "\n" if user.total_social_credit > 0 else ""
            return (
                f"CON {t('welcome_back', lang, name=user.name)}\n"
                + t('opt_balance', lang) + "\n"
                + t('opt_roundup', lang) + "\n"
                + t('opt_care', lang) + "\n"
                + t('opt_trust', lang) + "\n"
                + t('opt_witness', lang) + "\n"
                + t('opt_communities', lang) + "\n"
                + t('opt_support', lang) + "\n"
                + repay_line
                + t('opt_more', lang) + "\n"
                + t('opt_exit', lang)
            )
        else:
            return (
                f"CON {t('welcome', lang)}\n"
                "1. Register\n"
                + t('opt_support', lang) + "\n"
                + t('opt_more', lang) + "\n"
                + t('opt_exit', lang)
            )

    top = steps[0]

    # ── Exit ─────────────────────────────────────────────────────────────────
    if top == '0':
        return f"END {t('goodbye', lang)}"

    # ── Unregistered user flows ───────────────────────────────────────────────
    if not user:
        if top == '7':
            return _support_flow(None, phone, steps, level, lang)
        if top == '9':
            return _more_options_flow(None, phone, steps, level, lang)
        return _register_flow(phone, steps, level, lang)

    # ── Registered user flows ─────────────────────────────────────────────────
    if top == '1':
        return _balance_flow(user, steps, level, lang)
    if top == '2':
        return _roundup_flow(user, steps, level, lang)
    if top == '3':
        return _request_care_flow(user, steps, level, lang)
    if top == '4':
        return _trust_score(user, lang)
    if top == '5':
        return _witness_flow(user, steps, level, lang)
    if top == '6':
        return _communities_flow(user, steps, level, lang)
    if top == '7':
        return _support_flow(user, user.phone, steps, level, lang)
    if top == '8':
        return _repay_flow(user, steps, level, lang)
    if top == '9':
        return _more_options_flow(user, user.phone, steps, level, lang)

    return f"END {t('invalid_opt', lang)}"


# ── language flow ─────────────────────────────────────────────────────────────

def _language_flow(user, phone: str, steps: list, level: int, lang: str) -> str:
    lang_names = {'1': 'en', '2': 'lg', '3': 'sw'}
    lang_saved_msgs = {'en': t('lang_saved', 'en'), 'lg': t('lang_saved', 'lg'), 'sw': t('lang_saved', 'sw')}

    if level == 1:
        return f"CON {t('lang_menu', lang)}"

    choice = steps[1].strip()
    if choice == '0':
        return f"END {t('goodbye', lang)}"
    if choice not in lang_names:
        return f"END {t('lang_invalid', lang)}"

    new_lang = lang_names[choice]
    # Persist language preference if user is registered
    if user:
        try:
            user.preferred_language = new_lang
            db.session.commit()
        except Exception:
            db.session.rollback()
    else:
        # For unregistered users, store in phone-keyed in-memory dict
        # (will persist after registration via region_prefix convention)
        pass

    return f"END {lang_saved_msgs[new_lang]}"


# ── sub-flows ─────────────────────────────────────────────────────────────────

def _register_flow(phone: str, steps: list, level: int, lang: str) -> str:
    if steps[0] != '1':
        return f"END {t('unreg_first', lang)}"

    if level == 1:
        return f"CON {t('reg_name', lang)}"

    name = steps[1].strip()
    if not name:
        return f"END {t('blank_name', lang)}"

    if level == 2:
        return f"CON {t('reg_pin', lang)}"

    pin = steps[2].strip()
    if not pin.isdigit() or not (4 <= len(pin) <= 6):
        return f"END {t('pin_4digits', lang)}"

    if level == 3:
        return f"CON {t('reg_referrer', lang)}"

    referrer_input = steps[3].strip()
    referrer = None
    if referrer_input and referrer_input != '0':
        referrer = User.query.filter_by(phone=_normalize_phone(referrer_input)).first()

    normalized = _normalize_phone(phone)
    if User.query.filter_by(phone=normalized).first():
        return f"END {t('already_reg', lang)}"

    user = User(
        phone=normalized,
        name=name,
        pin=pin,
        sub_wallet_balance=0.0,
        trust_score=0.5,
        region_prefix=normalized[:3],
        preferred_language=lang,
    )
    if referrer:
        user.referred_by = referrer.id

    db.session.add(user)
    db.session.commit()
    logger.info("USSD registration: phone={} name={} lang={}", normalized, name, lang)
    return f"END {t('reg_success', lang, name=name)}"


def _balance_flow(user: User, steps: list, level: int, lang: str) -> str:
    if level == 1:
        return f"CON {t('enter_pin', lang)}"
    pin = steps[1].strip()
    if pin != (user.pin or '1234'):
        return f"END {t('wrong_pin', lang)}"
    return _balance(user, lang)


def _balance(user: User, lang: str) -> str:
    state = SystemState.query.first()
    pool = state.communal_pool_balance if state else 0.0
    try:
        ceiling = compute_draw_ceiling(user.id)
    except TrustGraphError:
        ceiling = 0.0
    return (
        f"END {t('balance_title', lang)}\n"
        f"{t('sub_wallet', lang)}: KES {user.sub_wallet_balance:.2f}\n"
        f"{t('draw_ceiling', lang)}: KES {ceiling:.2f}\n"
        f"{t('social_credit', lang)}: KES {user.total_social_credit:.2f}\n"
        f"{t('communal_pool', lang)}: KES {pool:.2f}"
    )


def _repay_flow(user: User, steps: list, level: int, lang: str) -> str:
    if user.total_social_credit <= 0:
        return f"END {t('repay_none', lang)}"

    if level == 1:
        return f"CON {t('repay_intro', lang, credit=user.total_social_credit)}"

    if level == 2:
        pin = steps[1].strip()
        if pin != (user.pin or '1234'):
            return f"END {t('wrong_pin', lang)}"
        return f"CON {t('repay_enter', lang, bal=user.sub_wallet_balance, credit=user.total_social_credit)}"

    if level == 3:
        pin = steps[1].strip()
        if pin != (user.pin or '1234'):
            return f"END {t('wrong_pin', lang)}"
        try:
            repay_amt = float(steps[2].strip())
            if repay_amt <= 0:
                raise ValueError
        except (ValueError, IndexError):
            return f"END {t('invalid_amount', lang)}"

        if repay_amt > user.sub_wallet_balance:
            return f"END {t('repay_insuff', lang, bal=user.sub_wallet_balance)}"

        from models import Transaction, TrustEvent
        actual = min(repay_amt, user.total_social_credit)
        user.sub_wallet_balance -= actual
        old_credit = user.total_social_credit
        user.total_social_credit = max(0.0, user.total_social_credit - actual)
        improvement = min(0.05, actual / 100_000 * 0.1)
        old_score = user.trust_score
        user.trust_score = min(1.0, user.trust_score + improvement)

        from models import db
        db.session.add(TrustEvent(
            user_id=user.id,
            old_score=old_score,
            new_score=user.trust_score,
            delta=round(improvement, 6),
            reason='debt_repayment',
        ))
        db.session.add(Transaction(
            user_id=user.id,
            amount=-actual,
            type='debt_repayment',
            description=f'USSD social credit repayment of UGX {actual:,.0f}',
        ))
        db.session.commit()

        logger.info(
            "USSD repayment: user_id={} repaid={:.0f} remaining_credit={:.0f} "
            "trust: {:.4f} → {:.4f}",
            user.id, actual, user.total_social_credit, old_score, user.trust_score,
        )
        return f"END {t('repay_done', lang, amt=actual, credit=user.total_social_credit, score=user.trust_score, old=old_score)}"

    return f"END {t('session_err', lang)}"


def _roundup_flow(user: User, steps: list, level: int, lang: str) -> str:
    wallet_pct = int(os.getenv('ROUNDUP_WALLET_PCT', 70))
    pool_pct   = int(os.getenv('ROUNDUP_POOL_PCT',   20))
    fee_pct    = 100 - wallet_pct - pool_pct

    if level == 1:
        return (
            f"CON {t('enter_amount', lang)}\n"
            f"{t('split_info', lang, w=wallet_pct, p=pool_pct, f=fee_pct)}"
        )

    try:
        amount = float(steps[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        return f"END {t('invalid_amount', lang)}"

    round_up = round(round(amount) - amount, 4)
    if round_up <= 0:
        round_up = 0.01

    w = wallet_pct / 100
    p = pool_pct   / 100
    to_wallet = round(round_up * w, 4)
    to_pool   = round(round_up * p, 4)
    to_fee    = round(round_up - to_wallet - to_pool, 4)

    user.sub_wallet_balance += to_wallet

    from models import Transaction, Community, CommunityMembership
    primary_comm = Community.query.get(user.primary_community_id) if user.primary_community_id else None
    if primary_comm and to_pool > 0:
        primary_comm.pool_balance += to_pool

    db.session.add(Transaction(user_id=user.id, amount=to_wallet, type='roundup',
                               description=f'USSD round-up wallet share from KES {amount:.2f}'))
    if to_pool > 0 and primary_comm:
        db.session.add(Transaction(user_id=user.id, amount=to_pool, type='pool_contribution',
                                   description=f'USSD round-up pool share from KES {amount:.2f}'))
    if to_fee > 0:
        db.session.add(Transaction(user_id=user.id, amount=to_fee, type='platform_fee',
                                   description=f'USSD round-up fee from KES {amount:.2f}'))
    db.session.commit()
    logger.info("USSD round-up: user_id={} total={:.4f} wallet={} pool={} fee={}",
                user.id, round_up, to_wallet, to_pool, to_fee)
    pool_info = (f"\n" + t('pool_credit', lang, amt=f'{to_pool:.2f}')) if to_pool > 0 else ""
    return (
        f"END {t('roundup_done', lang)}\n"
        + t('wallet_credit', lang, amt=f'{to_wallet:.2f}') + pool_info + "\n"
        + t('new_balance', lang, bal=f'{user.sub_wallet_balance:.2f}')
    )


def _request_care_flow(user: User, steps: list, level: int, lang: str) -> str:
    try:
        ceiling = compute_draw_ceiling(user.id)
    except TrustGraphError as exc:
        logger.error("USSD request_care TrustGraphError: {}", exc)
        return f"END {t('session_err', lang)}"

    if level == 1:
        return f"CON {t('care_ceiling', lang, ceiling=ceiling)}"

    try:
        needed = float(steps[1])
        if needed <= 0:
            raise ValueError
    except ValueError:
        return f"END {t('invalid_amount', lang)}"

    if needed > ceiling:
        return f"END {t('care_exceed', lang, ceiling=ceiling)}"

    if level == 2:
        return f"CON {t('care_provider', lang)}"

    provider_code = steps[2].strip().upper()
    from models import Provider
    provider_obj = Provider.query.filter_by(provider_code=provider_code, verified=True).first()
    if not provider_obj:
        all_providers = Provider.query.filter_by(verified=True).limit(3).all()
        examples = ', '.join(p.provider_code for p in all_providers) or 'MULAGO001'
        return f"END {t('care_bad_prov', lang, code=provider_code, examples=examples)}"
    provider_id = provider_code

    from_sub = min(user.sub_wallet_balance, needed)
    remaining = needed - from_sub
    user.sub_wallet_balance -= from_sub

    state = SystemState.query.first()
    from_pool = 0.0
    social_credit = 0.0
    if remaining > 0 and state:
        allowed = min(remaining, ceiling - from_sub, state.communal_pool_balance)
        from_pool = max(allowed, 0.0)
        state.communal_pool_balance -= from_pool
        social_credit = remaining - from_pool
        if social_credit > 0:
            user.total_social_credit += social_credit
            from recovery import update_recovery_parameters
            update_recovery_parameters(user.id, social_credit)

    from models import WitnessRequest
    from witness import select_witnesses, WitnessSelectionError
    try:
        witnesses = select_witnesses(user.id, provider_id)
    except WitnessSelectionError:
        witnesses = []

    req = WitnessRequest(
        user_id=user.id,
        needed_amount=needed,
        provider_id=provider_id,
        from_sub=from_sub,
        from_pool=from_pool,
        social_credit=social_credit,
        status='pending',
        witness_ids=','.join(str(w.id) for w in witnesses),
    )
    db.session.add(req)
    db.session.commit()

    try:
        from notifications import notify_witnesses_assigned
        notify_witnesses_assigned(user.name, needed, witnesses)
    except Exception:
        pass

    ceiling_remaining = max(0.0, ceiling - from_pool)
    logger.info(
        "USSD care request: user_id={} needed={} from_sub={} from_pool={} social_credit={}",
        user.id, needed, from_sub, from_pool, social_credit,
    )
    return f"END {t('care_done', lang, sub=from_sub, pool=from_pool, ceil=ceiling_remaining, rid=req.id)}"


def _provider_check_flow(steps: list, level: int, lang: str) -> str:
    if level == 1:
        return f"CON {t('prov_enter', lang)}"

    provider_code = steps[1].strip().upper() if len(steps) > 1 else ''
    if not provider_code:
        return f"CON {t('prov_enter', lang)}"

    from models import Provider, PaymentRecord
    provider = Provider.query.filter_by(provider_code=provider_code).first()
    if not provider:
        all_p = Provider.query.filter_by(verified=True).limit(3).all()
        examples = ', '.join(p.provider_code for p in all_p) or 'MULAGO001'
        return f"END {t('prov_bad', lang, code=provider_code, examples=examples)}"

    payments = PaymentRecord.query.filter_by(provider_id=provider.id)\
                .order_by(PaymentRecord.created_at.desc()).limit(5).all()
    if not payments:
        return f"END {t('prov_none', lang, name=provider.name)}"

    lines = [t('prov_title', lang, name=provider.name, n=len(payments))]
    for p in payments:
        lines.append(f"KES {p.amount:.0f} [{p.status}] {p.created_at.strftime('%d/%m')}")
    return "END " + "\n".join(lines)


def _trust_score(user: User, lang: str) -> str:
    try:
        ceiling = compute_draw_ceiling(user.id)
    except TrustGraphError:
        ceiling = 0.0
    return (
        f"END {t('trust_title', lang)}\n"
        f"{t('trust_score', lang)}: {user.trust_score:.2f}\n"
        f"{t('witness_acc', lang)}: {user.witness_accuracy_score:.2f}\n"
        f"{t('draw_ceiling', lang)}: KES {ceiling:.2f}\n"
        f"{t('roundup_mult', lang)}: {user.roundup_intensifier:.2f}x"
    )


def _help_faq(steps: list, level: int, lang: str) -> str:
    if level == 1:
        return f"CON {t('help_menu', lang)}"
    topic = steps[1] if len(steps) > 1 else ''
    if topic == '1':
        return f"END {t('help_1', lang)}"
    if topic == '2':
        return f"END {t('help_2', lang)}"
    if topic == '3':
        return f"END {t('help_3', lang)}"
    if topic == '4':
        return f"END {t('help_4', lang)}"
    if topic == '5':
        return f"END {t('help_5', lang)}"
    if topic == '0':
        return f"END {t('help_back', lang)}"
    return f"END {t('help_invalid', lang)}"


def _topup_flow(user: User, steps: list, level: int, lang: str) -> str:
    if not (os.getenv('MPESA_CONSUMER_KEY') and os.getenv('MPESA_CONSUMER_SECRET')):
        return f"END {t('no_mpesa', lang)}"

    if level == 1:
        return f"CON {t('topup_enter', lang)}"

    try:
        topup_amount = float(steps[1])
        if topup_amount < 1:
            raise ValueError("Too small")
    except (ValueError, IndexError):
        return f"END {t('invalid_amount', lang)}"

    phone = user.phone
    try:
        result = stk_push(
            phone=phone,
            amount=topup_amount,
            account_reference='SolidarityPool',
            description=f'USSD top-up for {user.name}',
        )
    except MpesaError as exc:
        logger.error("USSD STK push failed for user_id={}: {}", user.id, exc)
        return f"END {t('topup_fail', lang)}"

    checkout_id = result.get('CheckoutRequestID', '')
    merchant_id = result.get('MerchantRequestID', '')

    topup = MpesaTopup(
        user_id=user.id,
        amount=topup_amount,
        checkout_request_id=checkout_id,
        merchant_request_id=merchant_id,
        status='pending',
    )
    db.session.add(topup)
    db.session.commit()

    logger.info(
        "USSD STK push initiated: user_id={} phone={} amount={} checkout_id={}",
        user.id, phone, topup_amount, checkout_id,
    )
    return f"END {t('topup_sent', lang, phone=phone, amt=int(topup_amount))}"


# ── witness tasks flow (option 5) ─────────────────────────────────────────────

def _witness_flow(user: 'User', steps: list, level: int, lang: str) -> str:
    """Option 5: View and vote on pending care request witness tasks."""
    from models import CareRequest

    # Gather all CareRequests where this user is a witness and hasn't voted yet
    pending = []
    for cr in CareRequest.query.filter(CareRequest.status == 'pending_witness').all():
        if not cr.witness_ids:
            continue
        ids = [x.strip() for x in cr.witness_ids.split(',') if x.strip()]
        if str(user.id) not in ids:
            continue
        votes = [v for v in (cr.witness_votes or '').split(',') if v.strip()]
        already_voted = any(v.startswith(f"{user.id}:") for v in votes)
        if not already_voted:
            pending.append(cr)

    if level == 1:
        if not pending:
            return f"END {t('witness_none', lang)}"
        return f"CON {t('witness_count', lang, n=len(pending))}"

    if level == 2:
        pin = steps[1].strip()
        if pin != (user.pin or '1234'):
            return f"END {t('wrong_pin', lang)}"
        if not pending:
            return f"END {t('witness_none', lang)}"
        cr = pending[0]
        from models import User as _User
        req_user = _User.query.get(cr.user_id)
        req_name = req_user.name if req_user else 'Unknown'
        needed = getattr(cr, 'amount_needed', getattr(cr, 'needed_amount', 0))
        return f"CON {t('witness_item', lang, name=req_name, amount=needed)}"

    if level == 3:
        pin = steps[1].strip()
        if pin != (user.pin or '1234'):
            return f"END {t('wrong_pin', lang)}"
        if not pending:
            return f"END {t('witness_none', lang)}"
        cr = pending[0]
        vote_choice = steps[2].strip()
        if vote_choice == '0':
            return f"END {t('goodbye', lang)}"
        if vote_choice not in ('1', '2'):
            return f"END {t('invalid_opt', lang)}"
        response = 'accept' if vote_choice == '1' else 'reject'
        votes = [v for v in (cr.witness_votes or '').split(',') if v.strip()]
        if not any(v.startswith(f"{user.id}:") for v in votes):
            votes.append(f"{user.id}:{response}")
            cr.witness_votes = ','.join(votes)
            # Count accept/reject to determine final status
            accepts = sum(1 for v in votes if v.endswith(':accept'))
            rejects = sum(1 for v in votes if v.endswith(':reject'))
            total_witnesses = len([x for x in (cr.witness_ids or '').split(',') if x.strip()])
            if accepts >= 2:
                cr.status = 'approved'
            elif rejects >= 2 or (total_witnesses > 0 and rejects > total_witnesses / 2):
                cr.status = 'rejected'
            db.session.commit()
        logger.info("USSD witness vote: user_id={} care_req_id={} vote={}", user.id, cr.id, response)
        return f"END {t('witness_voted', lang)}"

    return f"END {t('session_err', lang)}"


# ── communities flow (option 6) ────────────────────────────────────────────────

def _ussd_leave_checks(user, comm, membership):
    """Return a string key for the blocking reason, or None if leave is allowed."""
    from models import CareRequest, FraudAlert, PaymentRecord

    # Sole admin
    if membership.role == 'admin':
        from models import CommunityMembership as CM
        other_admins = (CM.query
                        .filter_by(community_id=comm.id, role='admin')
                        .filter(CM.user_id != user.id)
                        .count())
        if other_admins == 0:
            return 'leave_blocked_admin'

    if user.total_social_credit > 0:
        return 'leave_blocked_credit'

    active_care = (CareRequest.query
                   .filter_by(user_id=user.id)
                   .filter(CareRequest.status.in_(['pending_witness', 'pending_admin', 'approved']))
                   .first())
    if active_care:
        return 'leave_blocked_care'

    open_fraud = FraudAlert.query.filter_by(user_id=user.id, resolved=False).first()
    if open_fraud:
        return 'leave_blocked_fraud'

    open_dispute = (PaymentRecord.query
                    .filter_by(user_id=user.id)
                    .filter(PaymentRecord.dispute_status.in_(['open', 'pending']))
                    .first())
    if open_dispute:
        return 'leave_blocked_dispute'

    return None


def _communities_flow(user: 'User', steps: list, level: int, lang: str) -> str:
    """Option 6: View user's community pool info, join, or request leave."""
    from models import Community, CommunityMembership

    # ── User already has a primary community ──────────────────────────────────
    if user.primary_community_id:
        comm = Community.query.get(user.primary_community_id)
        # Hide global reserve — treat as no community
        if comm and comm.is_global_reserve:
            comm = None

        if comm:
            membership = CommunityMembership.query.filter_by(
                user_id=user.id, community_id=comm.id
            ).first()
            target = comm.pool_target or 1
            pct = min(100.0, comm.pool_balance / target * 100)
            member_count = CommunityMembership.query.filter_by(community_id=comm.id).count()

            if level == 1:
                return f"CON {t('community_info', lang, name=comm.name, pool=comm.pool_balance, pct=pct, members=member_count)}"

            choice = steps[1].strip()

            if level == 2:
                if choice == '0':
                    return f"END {t('goodbye', lang)}"
                if choice == '1':
                    # Request leave — show confirmation
                    if membership and membership.leave_status == 'pending':
                        return f"END {t('leave_already', lang)}"
                    if membership:
                        block = _ussd_leave_checks(user, comm, membership)
                        if block:
                            return f"END {t(block, lang)}"
                    return f"CON {t('leave_confirm', lang, name=comm.name)}"
                return f"END {t('goodbye', lang)}"

            if level == 3:
                choice3 = steps[2].strip()
                if choice3 == '1' and steps[1].strip() == '1':
                    # Confirmed — submit leave request
                    if not membership:
                        return f"END {t('session_err', lang)}"
                    if membership.leave_status == 'pending':
                        return f"END {t('leave_already', lang)}"
                    block = _ussd_leave_checks(user, comm, membership)
                    if block:
                        return f"END {t(block, lang)}"
                    membership.leave_requested_at = datetime.utcnow()
                    membership.leave_status = 'pending'
                    membership.leave_initiated_by = 'member'
                    membership.leave_reason = 'Requested via USSD'
                    membership.leave_rejection_reason = None
                    db.session.commit()
                    logger.info("USSD leave request: user_id={} community_id={}", user.id, comm.id)
                    return f"END {t('leave_requested', lang)}"
                # 2 = cancel, or any other input
                return f"END {t('goodbye', lang)}"

            return f"END {t('session_err', lang)}"

    # ── No community — browse and join ────────────────────────────────────────
    comms = Community.query.filter_by(is_global_reserve=False).limit(5).all()

    if level == 1:
        if comms:
            names = '\n'.join(f"{i+1}. {c.name}" for i, c in enumerate(comms))
            return f"CON Join a community:\n{names}\n0. Back"
        return f"CON No communities yet.\nCreate one at the web portal.\n0. Back"

    choice = steps[1].strip()

    if level == 2:
        if choice == '0':
            return f"END {t('goodbye', lang)}"
        idx = None
        try:
            idx = int(choice) - 1
        except ValueError:
            pass
        if idx is not None and 0 <= idx < len(comms):
            comm = comms[idx]
            existing = CommunityMembership.query.filter_by(
                user_id=user.id, community_id=comm.id
            ).first()
            if not existing:
                db.session.add(CommunityMembership(user_id=user.id, community_id=comm.id, role='member'))
                user.primary_community_id = comm.id
                db.session.commit()
            logger.info("USSD community join: user_id={} community_id={}", user.id, comm.id)
            return f"END {t('community_join_ok', lang, name=comm.name)}"
        return f"CON {t('community_join_prompt', lang)}"

    if level == 3:
        community_name = steps[2].strip()
        from sqlalchemy import func as _func
        comm = Community.query.filter(
            Community.is_global_reserve == False,
            _func.lower(Community.name).contains(community_name.lower())
        ).first()
        if not comm:
            return f"END {t('community_join_fail', lang, name=community_name)}"
        existing = CommunityMembership.query.filter_by(
            user_id=user.id, community_id=comm.id
        ).first()
        if not existing:
            db.session.add(CommunityMembership(user_id=user.id, community_id=comm.id, role='member'))
            if not user.primary_community_id:
                user.primary_community_id = comm.id
            db.session.commit()
        logger.info("USSD community join by name: user_id={} community={}", user.id, comm.name)
        return f"END {t('community_join_ok', lang, name=comm.name)}"

    return f"END {t('session_err', lang)}"


# ── support & help flow (option 7) ────────────────────────────────────────────

def _support_flow(user, phone: str, steps: list, level: int, lang: str) -> str:
    """Option 7: Support tickets + FAQ. user may be None for unregistered callers."""
    from models import SupportTicket, SupportMessage

    if level == 1:
        return f"CON {t('support_menu', lang)}"

    choice = steps[1].strip()

    if choice == '0':
        return f"END {t('goodbye', lang)}"

    # ── FAQ sub-flow (choice 3) ────────────────────────────────────────────────
    if choice == '3':
        return _help_faq(steps[1:], level - 1, lang)

    # ── My tickets (choice 2) ─────────────────────────────────────────────────
    if choice == '2':
        if user is None:
            return f"END {t('unreg_first', lang)}"
        tickets = SupportTicket.query.filter_by(user_id=user.id)\
            .order_by(SupportTicket.updated_at.desc()).limit(4).all()
        if not tickets:
            return f"END {t('support_my_none', lang)}"
        lines = [t('support_my_title', lang)]
        for tk in tickets:
            lines.append(f"#{tk.id}: {(tk.subject or '')[:22]} [{tk.status}]")
        return "END " + "\n".join(lines)

    # ── Open new ticket (choice 1) ────────────────────────────────────────────
    if choice == '1':
        if level == 2:
            return f"CON {t('support_new_prompt', lang)}"
        if level == 3:
            message = steps[2].strip()
            if not message or len(message) < 3:
                return f"END Please describe your issue in more detail."
            norm_phone = _normalize_phone(phone)
            uid = user.id if user else None
            ticket = SupportTicket(
                user_id=uid,
                phone=norm_phone,
                subject='USSD Support Request',
                status='open',
                priority='medium',
            )
            db.session.add(ticket)
            db.session.flush()
            db.session.add(SupportMessage(
                ticket_id=ticket.id,
                sender_type='user',
                sender_id=uid or 0,
                body=message[:500],
            ))
            db.session.commit()
            logger.info("USSD support ticket created: phone={} ticket_id={}", norm_phone, ticket.id)
            # Notify admins
            try:
                from models import GlobalAdmin, User as _User
                admin_phones = []
                for ga in GlobalAdmin.query.all():
                    au = _User.query.get(ga.user_id)
                    if au and au.phone:
                        admin_phones.append(au.phone)
                from notifications import notify_admin_new_support_ticket
                notify_admin_new_support_ticket(admin_phones, ticket.subject, ticket.id, norm_phone)
            except Exception:
                pass
            return f"END {t('support_submitted', lang, id=ticket.id)}"

    return f"END {t('invalid_opt', lang)}"


# ── more options flow (option 9) ──────────────────────────────────────────────

def _more_options_flow(user, phone: str, steps: list, level: int, lang: str) -> str:
    """Option 9: Sub-menu for provider check, M-Pesa top-up, language, admin."""
    from models import GlobalAdmin
    is_admin = False
    if user:
        is_admin = GlobalAdmin.query.filter_by(user_id=user.id).first() is not None

    if level == 1:
        menu_key = 'more_menu_admin' if is_admin else 'more_menu'
        return f"CON {t(menu_key, lang)}"

    choice = steps[1].strip()

    if choice == '0':
        return f"END {t('goodbye', lang)}"

    if choice == '1':
        # Provider payment check — delegate with shifted steps
        return _provider_check_flow(steps[1:], level - 1, lang)

    if choice == '2':
        # M-Pesa top-up — requires registered user
        if user is None:
            return f"END {t('unreg_first', lang)}"
        return _topup_flow(user, steps[1:], level - 1, lang)

    if choice == '3':
        # Change language
        return _language_flow(user, phone, steps[1:], level - 1, lang)

    if choice == '4' and is_admin:
        # Full interactive admin panel — pass remaining steps through
        return _admin_panel_flow(user, steps[2:] if len(steps) > 2 else [], lang)

    return f"END {t('invalid_opt', lang)}"


# ── USSD admin panel (interactive, multi-level) ────────────────────────────────

def _admin_panel_flow(user: 'User', steps: list, lang: str) -> str:
    """
    Interactive admin panel via USSD.
    Reached via More Options (9) → 4.

    Sub-menu options:
      1. Review & approve/reject the oldest pending care request
      2. Fraud alert summary
      3. Platform stats (counts)
      0. Exit
    """
    from models import GlobalAdmin, CareRequest, FraudAlert, SupportTicket, PaymentRecord, User as UserModel

    ga = GlobalAdmin.query.filter_by(user_id=user.id).first()
    if not ga:
        return f"END {t('admin_not_auth', lang)}"

    role = (ga.role or 'support').replace('_', ' ')

    # ── No choice yet → show main admin panel menu ────────────────────────────
    if not steps or steps[0] == '':
        care  = CareRequest.query.filter_by(status='pending_admin').count()
        fraud = FraudAlert.query.filter_by(resolved=False).count()
        sup   = SupportTicket.query.filter_by(status='open').count()
        try:
            dis = PaymentRecord.query.filter(
                PaymentRecord.dispute_status == 'open').count()
        except Exception:
            dis = 0
        return (
            f"CON Admin Panel [{role}]\n"
            f"Care: {care}  Fraud: {fraud}\n"
            f"Support: {sup}  Disputes: {dis}\n"
            "1. Review care request\n"
            "2. Fraud alerts\n"
            "3. Platform stats\n"
            "0. Exit"
        )

    choice    = steps[0].strip()
    sub_steps = steps[1:] if len(steps) > 1 else []

    if choice == '0':
        return f"END {t('goodbye', lang)}"

    # ── Option 1: Care request review ─────────────────────────────────────────
    if choice == '1':
        pending = CareRequest.query.filter_by(
            status='pending_admin').order_by(CareRequest.id).first()
        if not pending:
            return "END No care requests pending.\nAll caught up!"

        # First entry — show care request details
        if not sub_steps:
            member = UserModel.query.get(pending.user_id)
            name = (member.name[:14] if member else f'#{pending.user_id}')
            return (
                f"CON Care #{pending.id}\n"
                f"Member: {name}\n"
                f"Amt: UGX {pending.amount_needed:,.0f}\n"
                f"Provider: {pending.provider_code or 'N/A'}\n"
                "1. Approve\n"
                "2. Reject\n"
                "0. Skip"
            )

        action = sub_steps[0].strip()
        if action == '0':
            return f"END {t('goodbye', lang)}"

        if action == '1':
            pending.status = 'approved'
            pending.admin_id = user.id
            db.session.commit()
            _log_admin_action_ussd(user.id, 'ussd_approve_care',
                                   target_user_id=pending.user_id,
                                   details=f'Care #{pending.id} approved via USSD by {user.name}')
            member = UserModel.query.get(pending.user_id)
            return (
                f"END Care #{pending.id} APPROVED\n"
                f"Member: {member.name if member else pending.user_id}\n"
                f"UGX {pending.amount_needed:,.0f} authorised."
            )

        if action == '2':
            pending.status = 'rejected'
            pending.admin_id = user.id
            db.session.commit()
            _log_admin_action_ussd(user.id, 'ussd_reject_care',
                                   target_user_id=pending.user_id,
                                   details=f'Care #{pending.id} rejected via USSD by {user.name}')
            return f"END Care #{pending.id} REJECTED.\nMember will be notified."

        return f"END {t('invalid_opt', lang)}"

    # ── Option 2: Fraud alert summary ─────────────────────────────────────────
    if choice == '2':
        alerts = FraudAlert.query.filter_by(resolved=False).limit(4).all()
        if not alerts:
            return "END No open fraud alerts.\nPlatform looks clean!"
        lines = [f"END Fraud Alerts ({len(alerts)} open):"]
        for fa in alerts:
            u = UserModel.query.get(fa.user_id)
            name = (u.name[:12] if u else f'#{fa.user_id}')
            lines.append(f"- {name}: {getattr(fa, 'risk_level', 'medium') or 'medium'}")
        lines.append("\nResolve at: /admin/fraud-alerts")
        return "\n".join(lines)

    # ── Option 3: Platform stats ───────────────────────────────────────────────
    if choice == '3':
        return _admin_stats_ussd(user, role, lang)

    return f"END {t('invalid_opt', lang)}"


def _admin_stats_ussd(user: 'User', role: str, lang: str) -> str:
    """Compact platform stat read-out (END screen)."""
    from models import CareRequest, FraudAlert, SupportTicket, PaymentRecord
    try:
        from models import VerifiedProvider
        prov_count = VerifiedProvider.query.filter_by(
            verification_status='pending').count()
    except Exception:
        prov_count = 0

    care  = CareRequest.query.filter_by(status='pending_admin').count()
    fraud = FraudAlert.query.filter_by(resolved=False).count()
    sup   = SupportTicket.query.filter_by(status='open').count()
    try:
        dis = PaymentRecord.query.filter(
            PaymentRecord.dispute_status == 'open').count()
    except Exception:
        dis = 0

    return f"END {t('admin_summary', lang, role=role, care=care, fraud=fraud, prov=prov_count, sup=sup, dis=dis)}"


def _log_admin_action_ussd(admin_id: int, action: str,
                            target_user_id=None, details='') -> None:
    """Write an audit log row directly (avoids circular import with app.py)."""
    try:
        from models import AdminAuditLog
        log = AdminAuditLog(
            admin_id=admin_id,
            target_user_id=target_user_id,
            action=action,
            details=details,
            ip='ussd',
            timestamp=datetime.utcnow(),
        )
        db.session.add(log)
        db.session.commit()
    except Exception as exc:
        logger.warning(f"USSD admin audit log failed: {exc}")
