# Проверка контрольных сумм MS43

Сформировано: 2026-10-06 06:54 UTC

| файл | тип | boot | prog | cal | _mon (unverified) |
| --- | --- | --- | --- | --- | --- |
| Siemens_MS43_MS430069_E36Z3_M54B30_EU4_LHD.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA61EB841 |
| Siemens_MS43_MS430069_E39_M54B25_EU4_LHD.bin | full512 | OK | OK | FAIL (0x5ADF vs 0xC138) | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA6202B49 |
| Siemens_MS43_MS430069_E39_M54B30_EU4_LHD.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA61EB841 |
| Siemens_MS43_MS430069_E39_M54B30_US.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA61EB841 |
| Siemens_MS43_MS430069_E46_Compact_M54B25_EU4_LHD.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA6202B49 |
| Siemens_MS43_MS430069_E46_M52TUB28_EU4_LHD.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA6202B49 |
| Siemens_MS43_MS430069_E46_M54B22_EU4_LHD.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA628D225 |
| Siemens_MS43_MS430069_E46_M54B22_EU4_RHD.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA628D225 |
| Siemens_MS43_MS430069_E46_M54B25_EU4_LHD.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA6202B49 |
| Siemens_MS43_MS430069_E46_M54B25_EU4_RHD.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA6202B49 |
| Siemens_MS43_MS430069_E46_M54B25_US.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA6202B49 |
| Siemens_MS43_MS430069_E46_M54B30_EU2_LHD.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA61EB8D5 |
| Siemens_MS43_MS430069_E46_M54B30_EU2_RHD.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA61EB8D5 |
| Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA61EB8D5 |
| Siemens_MS43_MS430069_E46_M54B30_EU4_RHD.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA61EB8D5 |
| Siemens_MS43_MS430069_E46_M54B30_US.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA61EB841 |
| Siemens_MS43_MS430069_E53_M54B30_EU4.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA61EB841 |
| Siemens_MS43_MS430069_E53_M54B30_US.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3DAAD17, cal_mon @ 0x72FFC = 0xA61EB841 |
| E39_525__C256C528_Ca430056_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA628D225 |
| E39_525__C256b50f_Ca430056_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA62015EF |
| E39_525__b137b56g_Ca430037_2.5L_Stok.bin | full512 | OK | FAIL (0x06C1 vs 0xF63D) | OK | prog_mon @ 0x6FDAE = 0xB227F7DA, cal_mon @ 0x72FFC = 0xA61DA371 |
| E39_525__b137b57g_Ca430037_Stok.bin | full512 | OK | FAIL (0x0830 vs 0xF294) | OK | prog_mon @ 0x6FDAE = 0xB227F938, cal_mon @ 0x72FFC = 0xA61DA371 |
| E39_525__c256b52d_430056_Stok.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA62015EF |
| E39_525__c256z50h_430056_Stok.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61EA2E7 |
| E39_530__C256J508_Ca430056_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61EA2E7 |
| E39_530__b137c56d_430037_Stok.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA6265FA4 |
| E39_530__b137j54g_Ca430037_Stok.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB227F938, cal_mon @ 0x72FFC = 0xA61C3069 |
| E39_530__c6668528_Ca430066_Stok.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3D92978, cal_mon @ 0x72FFC = 0xA628D225 |
| E46_325i__c155752k_Ca430055_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA62015EF |
| E46_330i__C155652L_Ca430055_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61EA2E7 |
| E46_330i__C155G50G_Ca430055_Stok.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3C037F4, cal_mon @ 0x72FFC = 0xA61EA2E7 |
| E46_330i__b137650h_Ca430037_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61C306C |
| E46_330i__b137652g_Ca430037_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61C3069 |
| E46_330i__b137654_Ca430037_Stok.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61C3069 |
| E46_330i__b137856g_Ca430037_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA6265FA4 |
| E46_330i__b137954g_Ca430037_Stok.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB227F7DA, cal_mon @ 0x72FFC = 0xA61C3069 |
| E46_330i__c256954g_Ca430056_Stok.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3BE7676, cal_mon @ 0x72FFC = 0xA61EA2E7 |
| E46_330i__c464650r_Ca430064_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61EB841 |
| E46_330i__c464850f_Ca430064_Stok.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA628D225 |
| E46_330i__c5666508_Ca430066_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61EA2E7 |
| E46_330i__c6669528_Ca430066_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61EB841 |
| E53__C356450h_Ca430056_stok.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61EB841 |
| E53__C769X54J_Ca430069_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61EB841 |
| E53__E39_b137b56g_Ca430037_2.5L_Full_Stok.bin | full512 | OK | FAIL (0x06C1 vs 0xF63D) | OK | prog_mon @ 0x6FDAE = 0xB227F7DA, cal_mon @ 0x72FFC = 0xA61DA371 |
| E53__c256454k_Ca430056_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61EA2E7 |
| E53__c356750c_Ca430056_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA6202B49 |
| E53__c356750f_Ca430056_Stok.bin | full512 | OK | OK | OK | prog_mon @ 0x6FDAE = 0xB3BE7676, cal_mon @ 0x72FFC = 0xA6202B49 |
| E53__c464x53j_Ca430064_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61EB841 |
| E53__c566450f_Ca430066_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61EB841 |
| E53__c566450k_Ca430066_Stok_Cal.bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA61EB841 |
| E39_525__b137c56g_430037_Stok.Bin | cal64 | -- | -- | OK | cal_mon @ 0x2FFC = 0xA6265FA4 |

Итог: 47 из 51 образов сходятся полностью.

## Расхождения

* `Siemens_MS43_MS430069_E39_M54B25_EU4_LHD.bin` / cal: хранимая 0x5ADF, расчётная 0xC138 (init 0x3936) -- известное
  * причина: калибровка сообщества: отличается от E46_M54B25 тремя байтами (0x70AB1, 0x70B1A, 0x70B1E), а хранимая сумма оставлена от исходной калибровки (0x5ADF) -> провал проверки. Файл заявлен как сток, но в него правки внесены без пересчёта CRC.
* `E39_525__b137b56g_Ca430037_2.5L_Stok.bin` / prog: хранимая 0x06C1, расчётная 0xF63D (init 0x3031) -- известное
  * причина: поле кода прошивки @ 0x6FFB6 = '10' (init 0x3031), но хранимая сумма не сходится ни с одним ASCII-init: содержимое prog меняли после расчёта CRC.
* `E39_525__b137b57g_Ca430037_Stok.bin` / prog: хранимая 0x0830, расчётная 0xF294 (init 0x3033) -- известное
  * причина: поле кода прошивки @ 0x6FFB6 = '30' (init 0x3033), но хранимая сумма не сходится: содержимое prog меняли после расчёта CRC.
* `E53__E39_b137b56g_Ca430037_2.5L_Full_Stok.bin` / prog: хранимая 0x06C1, расчётная 0xF63D (init 0x3031) -- известное
  * причина: побайтный дубликат E39_525__b137b56g_Ca430037_2.5L_Stok.bin, то же расхождение prog-суммы.
