# MS410DS1: jobs (из PRG через EdiabasLib, оффлайн)

Формат: `JOB` — комментарий (нем.) — аргументы — результаты (до 12).

* `EDIC_RESET` — EDIC-Reset — args: — — results: JOB_STATUS
* `INITIALISIERUNG` — — — args: — — results: 
* `INFO` — Information SGBD — args: — — results: ECU, ORIGIN, REVISION, AUTHOR, COMMENT, SPRACHE
* `ISN_LESEN` — — — args: — — results: ISN_LESEN_WERT, JOB_STATUS
* `RAM_LESEN` — Beliebige RAM - Zellen auslesen — args: RAM_LESEN_ADRESSE:long, RAM_LESEN_ANZAHL_BYTE:int — results: JOB_STATUS, RAM_LESEN_WERT, RAM_LESEN_EINH
* `ROM_LESEN` — Beliebige EPROM - Zellen auslesen — args: ROM_LESEN_ADRESSE:long, ROM_LESEN_ANZAHL_BYTE:int — results: JOB_STATUS, ROM_LESEN_WERT, ROM_LESEN_EINH
* `IDENT` — Ident-Daten fuer DME — args: — — results: JOB_STATUS, ID_BMW_NR, ID_HW_NR, ID_COD_INDEX, ID_DIAG_INDEX, ID_BUS_INDEX, ID_DATUM_KW, ID_DATUM_JAHR, ID_LIEF_NR, ID_SW_NR, ID_AI_NR, ID_PROD_NR …(+7)
* `ADAPT_LOESCHEN` — Adaptionen loeschen — args: — — results: JOB_STATUS
* `ABGAS_VARIANTE_LESEN` — Auslesen der Abgasvariante — args: — — results: JOB_STATUS, ABGAS_VARIANTE_WERT
* `STATUS_CODIER_CHECKSUMME` — Codier - Checksumme abfragen — args: — — results: JOB_STATUS, STATUS_CHECKSUMME_WERT
* `AIF_LESEN` — Auslesen des Anwender-Info-Feldes — args: — — results: JOB_STATUS, AIF_FG_NR, AIF_DATUM, AIF_AENDERUNGS_INDEX, AIF_SW_NR, AIF_BEHOERDEN_NR, AIF_ZB_NR
* `FS_LESEN` — Auslesen des Fehlerspeichers — args: — — results: JOB_STATUS, F_ANZ_NR, F_ORT_NR, F_ORT_TEXT, F_HFK, F_LOGISTIK, F_LZ, F_ART_ANZ, F_UW_ANZ, F_ART1_NR, F_ART1_TEXT, F_ART2_NR …(+31)
* `FS_LOESCHEN` — Loeschen des Fehlerspeichers — args: — — results: JOB_STATUS
* `FS_QUICK_LESEN` — Auslesen des QUICK Fehlerspeichers — args: — — results: JOB_STATUS, F_ANZ_NR, F_BSZ_AKT, F_BSZ_ALT
* `FS_SHADOW_LESEN` — Auslesen des Fehlerspeichers — args: — — results: JOB_STATUS, F_ANZ_NR, F_ORT_NR, F_ORT_TEXT, F_HFK, F_LOGISTIK, F_LZ, F_ART_ANZ, F_UW_ANZ, F_ART1_NR, F_ART1_TEXT, F_ART2_NR …(+31)
* `STATUS_MOTORDREHZAHL` — Motordrehzahl auslesen — args: — — results: JOB_STATUS, STATUS_MOTORDREHZAHL_WERT, STAT_MOTORDREHZAHL_WERT, STATUS_MOTORDREHZAHL_EINH
* `STATUS_MOTORTEMPERATUR` — Motortemperatur auslesen — args: — — results: JOB_STATUS, STATUS_MOTORTEMPERATUR_WERT, STAT_MOTORTEMPERATUR_WERT, STATUS_MOTORTEMPERATUR_EINH
* `STATUS_AN_LUFTTEMPERATUR` — Ansauglufttemperatur auslesen — args: — — results: JOB_STATUS, STATUS_AN_LUFTTEMPERATUR_WERT, STAT_AN_LUFTTEMPERATUR_WERT, STATUS_AN_LUFTTEMPERATUR_EINH
* `STATUS_GESCHWINDIGKEIT` — Fahrzeuggeschwindigkeit — args: — — results: JOB_STATUS, STATUS_GESCHWINDIGKEIT_WERT, STATUS_GESCHWINDIGKEIT_EINH
* `STATUS_DKP_VOLT` — Drosselklappenstellung  auslesen — args: — — results: JOB_STATUS, STATUS_DKP_VOLT_WERT, STATUS_DKP_VOLT_EINH
* `STATUS_UBATT` — Batteriespannung — args: — — results: JOB_STATUS, STATUS_UBATT_WERT, STAT_UBATT_WERT, STATUS_UBATT_EINH
* `STATUS_ZSR` — ZSR Spannung — args: — — results: JOB_STATUS, STATUS_ZSR_WERT, STATUS_ZSR_EINH
* `STATUS_LMM_VOLT` — HLM Spannung — args: — — results: JOB_STATUS, STATUS_LMM_VOLT_WERT, STATUS_LMM_VOLT_EINH
* `STATUS_KLOPF_ADC1` — ADC Spannung Klopfsensor 1 — args: — — results: JOB_STATUS, STATUS_KLOPF_ADC1_WERT, STATUS_KLOPF_ADC1_EINH
* `STATUS_KLOPF_ADC2` — ADC Spannung Klopfsensor 2 — args: — — results: JOB_STATUS, STATUS_KLOPF_ADC2_WERT, STATUS_KLOPF_ADC2_EINH
* `STATUS_L_SONDE` — Spannug Lambdasonde vor KAT auslesen — args: — — results: JOB_STATUS, STATUS_L_SONDE_WERT, STATUS_L_SONDE_EINH
* `STATUS_L_SONDE_2` — Spannug Lambdasonde vor KAT auslesen Sonde 2 — args: — — results: JOB_STATUS, STATUS_L_SONDE_2_WERT, STATUS_L_SONDE_2_EINH
* `STATUS_LMM` — Wert Luftmasse  auslesen — args: — — results: JOB_STATUS, STATUS_LMM_WERT, STATUS_LMM_EINH
* `STATUS_LAST` — Wert Last  auslesen — args: — — results: JOB_STATUS, STATUS_LAST_WERT, STAT_LAST_WERT, STATUS_LAST_EINH
* `STATUS_ZUENDWINKEL` — Wert Zuendwinkel  auslesen — args: — — results: JOB_STATUS, STATUS_ZUENDWINKEL_WERT, STAT_ZUENDWINKEL_WERT, STATUS_ZUENDWINKEL_EINH
* `STATUS_NW_POSITION` — Wert Nockenwellenposition  auslesen — args: — — results: JOB_STATUS, STATUS_NW_POSITION_WERT, STATUS_NW_POSITION_EINH
* `STATUS_EINSPRITZZEIT` — Wert Einspritzzeit auslesen — args: — — results: JOB_STATUS, STATUS_EINSPRITZZEIT_WERT, STAT_EINSPRITZZEIT_WERT, STATUS_EINSPRITZZEIT_EINH
* `STATUS_LL_REGLER` — Wert LL-REGLER auslesen — args: — — results: JOB_STATUS, STATUS_LL_REGLER_WERT, STATUS_LL_REGLER_EINH
* `STATUS_H_SONDE` — Lambdasondenheizung  1  auslesen — args: — — results: JOB_STATUS, STATUS_H_SONDE_WERT, STATUS_H_SONDE_EINH
* `STATUS_H_SONDE_2` — Lambdasondenheizung  2  auslesen — args: — — results: JOB_STATUS, STATUS_H_SONDE_2_WERT, STATUS_H_SONDE_2_EINH
* `STATUS_INT` — INT-Signal — args: — — results: JOB_STATUS, STATUS_INT_WERT, STATUS_INT_EINH
* `STATUS_INT_2` — INT-Signal Bank 2 — args: — — results: JOB_STATUS, STATUS_INT_2_WERT, STATUS_INT_2_EINH
* `STATUS_ADD` — ADD-Signal — args: — — results: JOB_STATUS, STATUS_ADD_WERT, STATUS_ADD_EINH
* `STATUS_ADD_2` — ADD-Signal Bank 2 — args: — — results: JOB_STATUS, STATUS_ADD_2_WERT, STATUS_ADD_2_EINH
* `STATUS_MUL` — MUL-Signal — args: — — results: JOB_STATUS, STATUS_MUL_WERT, STATUS_MUL_EINH
* `STATUS_MUL_2` — MUL-Signal Bank 2 — args: — — results: JOB_STATUS, STATUS_MUL_2_WERT, STATUS_MUL_2_EINH
* `STATUS_TAST_LL_STELLER` — Wert Tastverhaeltnis LL-Steller auslesen — args: — — results: JOB_STATUS, STATUS_TAST_LL_STELLER_WERT, STATUS_TAST_LL_STELLER_EINH
* `STATUS_NW_IST_POSITION` — Wert Nockenwellenposition  auslesen — args: — — results: JOB_STATUS, STATUS_NW_IST_POSITION_WERT, STATUS_NW_IST_POSITION_EINH
* `STATUS_VANOS_FRUEH_VERST_ZEIT` — Wert Vanos Verstellzeit Frueh  auslesen — args: — — results: JOB_STATUS, STATUS_VANOS_FRUEH_VERST_ZEIT_WERT, STATUS_VANOS_FRUEH_VERST_ZEIT_EINH
* `STATUS_VANOS_SPAET_VERST_ZEIT` — Wert Vanos Verstellzeit Spaet  auslesen — args: — — results: JOB_STATUS, STATUS_VANOS_SPAET_VERST_ZEIT_WERT, STATUS_VANOS_SPAET_VERST_ZEIT_EINH
* `STATUS_K_MW_1` — Wert Ubat auslesen — args: — — results: JOB_STATUS, STATUS_K_MW_1_WERT, STATUS_K_MW_1_EINH
* `STATUS_K_MW_2` — Wert Ubat auslesen — args: — — results: JOB_STATUS, STATUS_K_MW_2_WERT, STATUS_K_MW_2_EINH
* `STATUS_K_MW_3` — Wert Ubat auslesen — args: — — results: JOB_STATUS, STATUS_K_MW_3_WERT, STATUS_K_MW_3_EINH
* `STATUS_K_MW_4` — Wert Ubat auslesen — args: — — results: JOB_STATUS, STATUS_K_MW_4_WERT, STATUS_K_MW_4_EINH
* `STATUS_K_MW_5` — Wert Ubat auslesen — args: — — results: JOB_STATUS, STATUS_K_MW_5_WERT, STATUS_K_MW_5_EINH
* `STATUS_K_MW_6` — Wert Ubat auslesen — args: — — results: JOB_STATUS, STATUS_K_MW_6_WERT, STATUS_K_MW_6_EINH
* `STATUS_VANOS_VERSTELLWINKEL` — Wert VANOS_VERSTELLWINKEL auslesen — args: — — results: JOB_STATUS, STATUS_VANOS_VERSTELLWINKEL_WERT, STATUS_VANOS_VERSTELLWINKEL_EINH
* `STATUS_LL_LUFTBEDARF` — Luftbedarf LL - Steller — args: — — results: JOB_STATUS, STATUS_LL_LUFTBEDARF_WERT, STATUS_LL_LUFTBEDARF_EINH
* `STATUS_LAMBDA_COUNTER` — Counter fuer die Einschaltbedingung Lambdasonde — args: — — results: JOB_STATUS, STATUS_LAMBDA_COUNTER_WERT, STATUS_LAMBDA_COUNTER_EINH
* `STATUS_DIGITAL` — Status Schalteingaenge — args: — — results: JOB_STATUS, STATUS_VL_EIN, STATUS_LL_EIN, STATUS_DWA_EIN, STATUS_FS_EIN, STATUS_KO_EIN, STAT_KO_EIN, STATUS_AC_EIN, STAT_AC_EIN, STATUS_GE_EIN, STATUS_LAMBDAREGELUNG_1_EIN, STATUS_LAMBDAREGELUNG_2_EIN …(+11)
* `STEUERN_EV_1` — EV  1 ansteuern — args: — — results: JOB_STATUS
* `STEUERN_EV_2` — EV  2 ansteuern — args: — — results: JOB_STATUS
* `STEUERN_EV_3` — EV 3 ansteuern — args: — — results: JOB_STATUS
* `STEUERN_EV_4` — EV 4 ansteuern — args: — — results: JOB_STATUS
* `STEUERN_EV_5` — EV  5 ansteuern — args: — — results: JOB_STATUS
* `STEUERN_EV_6` — EV  6 ansteuern — args: — — results: JOB_STATUS
* `STEUERN_TEV` — Tankentlueftungsventil  ansteuern — args: — — results: JOB_STATUS
* `STEUERN_LL_STELLER` — Leerlaufsteller ansteuern — args: — — results: JOB_STATUS
* `STEUERN_ABGASKLAPPE` — Abgasklappe ansteuern — args: — — results: JOB_STATUS
* `STEUERN_EKP` — EKP-Relais ansteuern — args: — — results: JOB_STATUS
* `STEUERN_KO` — Klimakompressorrelais ansteuern — args: — — results: JOB_STATUS
* `STEUERN_VANOS` — VANOS ansteuern — args: — — results: JOB_STATUS
* `DIAGNOSE_ENDE` — Loeschen des Fehlerspeichers — args: — — results: JOB_STATUS
* `UPROG_EIN` — Programmierspannung einschalten — args: — — results: JOB_STATUS, STATUS_UPROG_WERT, STATUS_UPROG_EINH
* `UPROG_AUS` — Programmierspannung ausschalten — args: — — results: JOB_STATUS
* `CO_ABGLEICH_LESEN` — CO-Abgleich lesen — args: — — results: JOB_STATUS, STATUS_CO_ABGLEICH_WERT, STATUS_CO_ABGLEICH_FREIE_ZELLEN
* `CO_ABGLEICH_VERSTELLEN` — CO-Abgleich lesen — args: CO_VERSTELL_WERT:int — results: JOB_STATUS
* `CO_ABGLEICH_PROGRAMMIEREN` — CO-Abgleich programmieren — args: — — results: JOB_STATUS, CO_ABGLEICH_PROGRAMMIEREN_STATUS
* `STATUS_LMM_MASSE` — Wert Luftmasse  auslesen — args: — — results: JOB_STATUS, STAT_LMM_MASSE_WERT, STAT_LMM_MASSE_EINH
* `STATUS_LS_VKAT_HEIZUNG_TV_1` — Lambdasondenheizung  1  auslesen — args: — — results: JOB_STATUS, STAT_LS_VKAT_HEIZUNG_TV_1_WERT, STAT_LS_VKAT_HEIZUNG_TV_1_EINH
* `STATUS_LS_VKAT_HEIZUNG_TV_2` — Lambdasondenheizung  2  auslesen — args: — — results: JOB_STATUS, STAT_LS_VKAT_HEIZUNG_TV_2_WERT, STAT_LS_VKAT_HEIZUNG_TV_2_EINH
* `STATUS_LS_VKAT_SIGNAL_1` — Spannug Lambdasonde vor KAT auslesen — args: — — results: JOB_STATUS, STAT_LS_VKAT_SIGNAL_1_WERT, STAT_LS_VKAT_SIGNAL_1_EINH
* `STATUS_LS_VKAT_SIGNAL_2` — Spannug Lambdasonde vor KAT auslesen Sonde 2 — args: — — results: JOB_STATUS, STAT_LS_VKAT_SIGNAL_2_WERT, STAT_LS_VKAT_SIGNAL_2_EINH
* `STATUS_LAMBDA_INTEGRATOR_1` — INT-Signal — args: — — results: JOB_STATUS, STAT_LAMBDA_INTEGRATOR_1_WERT, STAT_LAMBDA_INTEGRATOR_1_EINH
* `PRUEFSTEMPEL_LESEN` — Auslesen des Pruefstempels — args: — — results: JOB_STATUS, BYTE1, BYTE2, BYTE3
* `PRUEFSTEMPEL_SCHREIBEN` — Beschreiben des Pruefstempels Es muessen immer alle drei Argumente im Bereich von 0-255 bzw. 0x00-0xFF uebergeben werden. — args: BYTE1:int, BYTE2:int, BYTE3:int — results: JOB_STATUS
* `STATUS_LAMBDA_INTEGRATOR_2` — INT-Signal Bank 2 — args: — — results: JOB_STATUS, STAT_LAMBDA_INTEGRATOR_2_WERT, STAT_LAMBDA_INTEGRATOR_2_EINH
* `STATUS_LAMBDA_ADD_1` — ADD-Signal — args: — — results: JOB_STATUS, STAT_LAMBDA_ADD_1_WERT, STAT_LAMBDA_ADD_1_EINH
* `STATUS_LAMBDA_ADD_2` — ADD-Signal Bank 2 — args: — — results: JOB_STATUS, STAT_LAMBDA_ADD_2_WERT, STAT_LAMBDA_ADD_2_EINH
* `STATUS_LAMBDA_MUL_1` — MUL-Signal — args: — — results: JOB_STATUS, STAT_LAMBDA_MUL_1_WERT, STAT_LAMBDA_MUL_1_EINH
* `STATUS_LAMBDA_MUL_2` — MUL-Signal Bank 2 — args: — — results: JOB_STATUS, STAT_LAMBDA_MUL_2_WERT, STAT_LAMBDA_MUL_2_EINH
