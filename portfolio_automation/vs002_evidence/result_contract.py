"""Frozen result contract for the deterministic VS-002 result runner.

``experimental_noncanonical``. This module is PURE DATA + enums + the frozen
Student-t table. It performs no IO, no network, no package discovery, and
computes no experiment statistic — importing it has zero side effects. The
scientific computation lives in :mod:`result_runner`; the canonical frozen
scientific authority is ``evals/vertical_slice/VS-002_preregistration.json``.

The runner emits a :class:`VS002Result`. Its ``to_observations()`` renders a
strict-JSON dict suitable for embedding as the authority-screened
``observations`` payload of a canonical Northstar ``ExperimentResult`` (the same
pattern VS-001 uses). It carries no authority: ``grants_authority`` is always
False and ``vs002_executed`` reflects the real-execution semantics of the
artifact, never a claim made by this synthetic-certification mission.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Tuple

RESULT_SCHEMA_VERSION = "engineering.vs002_result.v1"
RESULT_SCHEMA_KIND = "experimental_noncanonical"
DEFAULT_RUNNER_ID = "vs002_evidence.result_runner"
RUNNER_VERSION = "v1"


# ── Frozen classification vocabularies (from binding_core.classification) ────
class CriterionOutcome(str, Enum):
    MET = "PREREGISTERED_CRITERIA_MET"
    NOT_MET = "PREREGISTERED_CRITERIA_NOT_MET"
    INCONCLUSIVE = "EVIDENCE_INCONCLUSIVE"


class SkillOutcome(str, Enum):
    PRESENT = "ECONOMIC_SKILL_EVIDENCE_PRESENT"
    NOT_ESTABLISHED = "ECONOMIC_SKILL_NOT_ESTABLISHED"


class H1Status(str, Enum):
    MET = "H1_MET"
    NOT_MET = "H1_NOT_MET"
    INCONCLUSIVE = "H1_INCONCLUSIVE"


class H2Status(str, Enum):
    MET = "H2_MET"
    NOT_MET = "H2_NOT_MET"
    INCONCLUSIVE = "H2_INCONCLUSIVE"


class NoActionStatus(str, Enum):
    BEAT = "NO_ACTION_BEAT"
    NOT_BEAT = "NO_ACTION_NOT_BEAT"
    INCONCLUSIVE = "NO_ACTION_INCONCLUSIVE"


# ── Deterministic tabulated two-sided 95% Student-t critical values t(0.975, df) ─
# The frozen interval rule mandates deterministic TABULATED Student-t 0.975
# critical values with NO scipy/statsmodels and NO normal-1.96 approximation.
#
# RUNTIME READS A COMMITTED IMMUTABLE STATIC TABLE ONLY: student_t_critical is a
# pure lookup and NEVER numerically generates a value. The reviewable pure-python
# generator that PRODUCED these values lives in
# scripts/generate_vs002_student_t_table.py (regularized-incomplete-beta
# inversion) and is used only for generation / provenance / verification, never
# at runtime. A governance test regenerates the whole table from that method and
# checks it against this committed one, alongside independent published anchors.
#
# The table certifies df 1..STUDENT_T_MAX_DF. This is CERTIFIED STATIC TABLE
# COVERAGE (implementation coverage), NOT a scientific maximum cohort count, and
# nothing about the real evidence package informed it. A future real VS-002
# execution must prove its required df = n-1 lies within this coverage before
# computing an interval; otherwise the runner FAILS CLOSED (StudentTTableError) --
# an engineering/certification blocker requiring a reviewed table-extension
# ruling, NOT an EVIDENCE_INCONCLUSIVE result.

# df=0 is an unused placeholder so STUDENT_T_0975[df] indexes by df.
STUDENT_T_0975: tuple = (
    None,
    12.70620474, 4.30265273, 3.18244631, 2.77644511, 2.57058184, 2.44691185, 2.36462425, 2.30600414,  # df 1-8
    2.26215716, 2.22813885, 2.20098516, 2.17881283, 2.16036866, 2.14478669, 2.13144955, 2.1199053,  # df 9-16
    2.10981558, 2.10092204, 2.09302405, 2.08596345, 2.07961384, 2.07387307, 2.06865761, 2.06389856,  # df 17-24
    2.05953855, 2.05552944, 2.05183052, 2.04840714, 2.04522964, 2.04227246, 2.03951345, 2.03693334,  # df 25-32
    2.0345153, 2.03224451, 2.03010793, 2.028094, 2.02619246, 2.02439416, 2.02269092, 2.02107539,  # df 33-40
    2.01954097, 2.0180817, 2.0166922, 2.01536757, 2.01410339, 2.0128956, 2.01174051, 2.01063476,  # df 41-48
    2.00957524, 2.00855911, 2.00758377, 2.00664681, 2.005746, 2.00487929, 2.00404478, 2.00324072,  # df 49-56
    2.00246546, 2.00171748, 2.00099538, 2.00029782, 1.99962358, 1.99897152, 1.99834054, 1.99772965,  # df 57-64
    1.99713791, 1.99656442, 1.99600835, 1.99546893, 1.99494542, 1.99443711, 1.99394337, 1.99346357,  # df 65-72
    1.99299713, 1.9925435, 1.99210215, 1.99167261, 1.9912544, 1.99084707, 1.99045021, 1.99006342,  # df 73-80
    1.98968632, 1.98931856, 1.98895978, 1.98860967, 1.98826791, 1.98793421, 1.98760828, 1.98728986,  # df 81-88
    1.9869787, 1.98667454, 1.98637715, 1.98608632, 1.98580181, 1.98552344, 1.985251, 1.98498431,  # df 89-96
    1.98472319, 1.98446745, 1.98421695, 1.98397152, 1.983731, 1.98349526, 1.98326414, 1.98303753,  # df 97-104
    1.98281527, 1.98259726, 1.98238337, 1.98217348, 1.98196749, 1.98176528, 1.98156676, 1.98137181,  # df 105-112
    1.98118036, 1.9809923, 1.98080754, 1.980626, 1.9804476, 1.98027225, 1.98009988, 1.97993041,  # df 113-120
    1.97976376, 1.97959988, 1.97943869, 1.97928012, 1.97912411, 1.9789706, 1.97881953, 1.97867085,  # df 121-128
    1.97852449, 1.97838041, 1.97823854, 1.97809884, 1.97796126, 1.97782576, 1.97769228, 1.97756078,  # df 129-136
    1.97743121, 1.97730354, 1.97717772, 1.97705372, 1.97693149, 1.97681099, 1.9766922, 1.97657507,  # df 137-144
    1.97645956, 1.97634565, 1.97623331, 1.97612249, 1.97601318, 1.97590533, 1.97579892, 1.97569393,  # df 145-152
    1.97559032, 1.97548806, 1.97538713, 1.97528751, 1.97518916, 1.97509207, 1.97499621, 1.97490156,  # df 153-160
    1.97480809, 1.97471579, 1.97462462, 1.97453458, 1.97444563, 1.97435776, 1.97427096, 1.97418519,  # df 161-168
    1.97410045, 1.97401671, 1.97393395, 1.97385217, 1.97377134, 1.97369144, 1.97361246, 1.97353439,  # df 169-176
    1.9734572, 1.97338089, 1.97330543, 1.97323082, 1.97315704, 1.97308408, 1.97301192, 1.97294054,  # df 177-184
    1.97286995, 1.97280011, 1.97273103, 1.97266269, 1.97259508, 1.97252818, 1.97246199, 1.97239649,  # df 185-192
    1.97233168, 1.97226753, 1.97220405, 1.97214122, 1.97207903, 1.97201748, 1.97195654, 1.97189622,  # df 193-200
    1.97183651, 1.97177738, 1.97171885, 1.97166089, 1.9716035, 1.97154667, 1.97149039, 1.97143466,  # df 201-208
    1.97137946, 1.97132479, 1.97127065, 1.97121701, 1.97116389, 1.97111126, 1.97105912, 1.97100747,  # df 209-216
    1.9709563, 1.9709056, 1.97085537, 1.97080559, 1.97075627, 1.9707074, 1.97065896, 1.97061096,  # df 217-224
    1.97056339, 1.97051624, 1.97046951, 1.97042319, 1.97037728, 1.97033177, 1.97028666, 1.97024194,  # df 225-232
    1.9701976, 1.97015364, 1.97011006, 1.97006685, 1.97002401, 1.96998153, 1.96993941, 1.96989764,  # df 233-240
    1.96985621, 1.96981513, 1.9697744, 1.96973399, 1.96969392, 1.96965418, 1.96961476, 1.96957565,  # df 241-248
    1.96953687, 1.96949839, 1.96946023, 1.96942237, 1.9693848, 1.96934754, 1.96931057, 1.96927389,  # df 249-256
    1.9692375, 1.96920139, 1.96916556, 1.96913, 1.96909472, 1.96905972, 1.96902497, 1.9689905,  # df 257-264
    1.96895628, 1.96892232, 1.96888862, 1.96885517, 1.96882197, 1.96878902, 1.96875631, 1.96872385,  # df 265-272
    1.96869162, 1.96865963, 1.96862787, 1.96859634, 1.96856505, 1.96853397, 1.96850313, 1.9684725,  # df 273-280
    1.96844209, 1.9684119, 1.96838192, 1.96835216, 1.9683226, 1.96829326, 1.96826411, 1.96823517,  # df 281-288
    1.96820644, 1.9681779, 1.96814955, 1.96812141, 1.96809345, 1.96806569, 1.96803811, 1.96801073,  # df 289-296
    1.96798353, 1.96795651, 1.96792967, 1.96790301, 1.96787653, 1.96785023, 1.9678241, 1.96779814,  # df 297-304
    1.96777235, 1.96774674, 1.96772129, 1.967696, 1.96767089, 1.96764593, 1.96762113, 1.9675965,  # df 305-312
    1.96757202, 1.9675477, 1.96752353, 1.96749952, 1.96747566, 1.96745195, 1.96742839, 1.96740497,  # df 313-320
    1.96738171, 1.96735859, 1.96733561, 1.96731277, 1.96729008, 1.96726752, 1.96724511, 1.96722283,  # df 321-328
    1.96720068, 1.96717867, 1.9671568, 1.96713506, 1.96711344, 1.96709196, 1.96707061, 1.96704938,  # df 329-336
    1.96702828, 1.96700731, 1.96698646, 1.96696573, 1.96694513, 1.96692465, 1.96690428, 1.96688404,  # df 337-344
    1.96686391, 1.9668439, 1.966824, 1.96680422, 1.96678456, 1.966765, 1.96674556, 1.96672623,  # df 345-352
    1.96670701, 1.9666879, 1.96666889, 1.96665, 1.9666312, 1.96661252, 1.96659394, 1.96657546,  # df 353-360
    1.96655709, 1.96653881, 1.96652064, 1.96650257, 1.9664846, 1.96646672, 1.96644895, 1.96643127,  # df 361-368
    1.96641368, 1.9663962, 1.9663788, 1.9663615, 1.9663443, 1.96632718, 1.96631016, 1.96629323,  # df 369-376
    1.96627639, 1.96625964, 1.96624297, 1.9662264, 1.96620991, 1.96619351, 1.96617719, 1.96616096,  # df 377-384
    1.96614481, 1.96612875, 1.96611277, 1.96609688, 1.96608106, 1.96606533, 1.96604968, 1.96603411,  # df 385-392
    1.96601861, 1.9660032, 1.96598787, 1.96597261, 1.96595743, 1.96594232, 1.9659273, 1.96591234,  # df 393-400
    1.96589747, 1.96588266, 1.96586793, 1.96585327, 1.96583869, 1.96582418, 1.96580974, 1.96579537,  # df 401-408
    1.96578107, 1.96576684, 1.96575268, 1.96573859, 1.96572457, 1.96571061, 1.96569673, 1.9656829,  # df 409-416
    1.96566915, 1.96565546, 1.96564184, 1.96562828, 1.96561479, 1.96560136, 1.965588, 1.9655747,  # df 417-424
    1.96556146, 1.96554828, 1.96553517, 1.96552211, 1.96550912, 1.96549619, 1.96548332, 1.96547051,  # df 425-432
    1.96545776, 1.96544506, 1.96543243, 1.96541985, 1.96540733, 1.96539487, 1.96538247, 1.96537012,  # df 433-440
    1.96535783, 1.96534559, 1.96533341, 1.96532128, 1.96530921, 1.9652972, 1.96528523, 1.96527332,  # df 441-448
    1.96526147, 1.96524966, 1.96523791, 1.96522622, 1.96521457, 1.96520297, 1.96519143, 1.96517993,  # df 449-456
    1.96516849, 1.9651571, 1.96514575, 1.96513446, 1.96512322, 1.96511202, 1.96510087, 1.96508977,  # df 457-464
    1.96507872, 1.96506772, 1.96505676, 1.96504585, 1.96503499, 1.96502417, 1.9650134, 1.96500268,  # df 465-472
    1.964992, 1.96498136, 1.96497077, 1.96496023, 1.96494973, 1.96493927, 1.96492886, 1.96491849,  # df 473-480
    1.96490816, 1.96489788, 1.96488764, 1.96487744, 1.96486729, 1.96485717, 1.9648471, 1.96483707,  # df 481-488
    1.96482708, 1.96481713, 1.96480722, 1.96479736, 1.96478753, 1.96477774, 1.96476799, 1.96475828,  # df 489-496
    1.96474861, 1.96473898, 1.96472939, 1.96471984, 1.96471032, 1.96470084, 1.96469141, 1.964682,  # df 497-504
    1.96467264, 1.96466331, 1.96465402, 1.96464477, 1.96463555, 1.96462637, 1.96461722, 1.96460811,  # df 505-512
    1.96459904, 1.96459, 1.964581, 1.96457203, 1.9645631, 1.9645542, 1.96454533, 1.9645365,  # df 513-520
    1.9645277, 1.96451894, 1.96451021, 1.96450152, 1.96449285, 1.96448422, 1.96447563, 1.96446706,  # df 521-528
    1.96445853, 1.96445003, 1.96444157, 1.96443313, 1.96442473, 1.96441635, 1.96440801, 1.9643997,  # df 529-536
    1.96439143, 1.96438318, 1.96437496, 1.96436678, 1.96435862, 1.96435049, 1.9643424, 1.96433433,  # df 537-544
    1.96432629, 1.96431829, 1.96431031, 1.96430236, 1.96429444, 1.96428655, 1.96427869, 1.96427086,  # df 545-552
    1.96426305, 1.96425527, 1.96424753, 1.9642398, 1.96423211, 1.96422445, 1.96421681, 1.9642092,  # df 553-560
    1.96420161, 1.96419406, 1.96418653, 1.96417903, 1.96417155, 1.9641641, 1.96415668, 1.96414928,  # df 561-568
    1.96414191, 1.96413456, 1.96412725, 1.96411995, 1.96411268, 1.96410544, 1.96409822, 1.96409103,  # df 569-576
    1.96408386, 1.96407672, 1.9640696, 1.96406251, 1.96405544, 1.9640484, 1.96404138, 1.96403438,  # df 577-584
    1.96402741, 1.96402046, 1.96401354, 1.96400664, 1.96399976, 1.9639929, 1.96398607, 1.96397927,  # df 585-592
    1.96397248, 1.96396572, 1.96395898, 1.96395226, 1.96394557, 1.9639389, 1.96393225, 1.96392562,  # df 593-600
    1.96391902, 1.96391243, 1.96390587, 1.96389933, 1.96389282, 1.96388632, 1.96387985, 1.96387339,  # df 601-608
    1.96386696, 1.96386055, 1.96385416, 1.96384779, 1.96384144, 1.96383512, 1.96382881, 1.96382252,  # df 609-616
    1.96381626, 1.96381001, 1.96380379, 1.96379758, 1.9637914, 1.96378523, 1.96377909, 1.96377296,  # df 617-624
    1.96376685, 1.96376077, 1.9637547, 1.96374865, 1.96374263, 1.96373662, 1.96373063, 1.96372465,  # df 625-632
    1.9637187, 1.96371277, 1.96370685, 1.96370096, 1.96369508, 1.96368922, 1.96368338, 1.96367756,  # df 633-640
    1.96367175, 1.96366597, 1.9636602, 1.96365445, 1.96364872, 1.963643, 1.96363731, 1.96363163,  # df 641-648
    1.96362597, 1.96362032, 1.9636147, 1.96360909, 1.96360349, 1.96359792, 1.96359236, 1.96358682,  # df 649-656
    1.9635813, 1.96357579, 1.9635703, 1.96356482, 1.96355937, 1.96355393, 1.9635485, 1.96354309,  # df 657-664
    1.9635377, 1.96353233, 1.96352697, 1.96352162, 1.9635163, 1.96351098, 1.96350569, 1.96350041,  # df 665-672
    1.96349514, 1.9634899, 1.96348466, 1.96347945, 1.96347424, 1.96346906, 1.96346389, 1.96345873,  # df 673-680
    1.96345359, 1.96344846, 1.96344335, 1.96343826, 1.96343318, 1.96342811, 1.96342306, 1.96341802,  # df 681-688
    1.963413, 1.96340799, 1.963403, 1.96339802, 1.96339306, 1.96338811, 1.96338318, 1.96337825,  # df 689-696
    1.96337335, 1.96336845, 1.96336358, 1.96335871, 1.96335386, 1.96334902, 1.9633442, 1.96333939,  # df 697-704
    1.96333459, 1.96332981, 1.96332504, 1.96332029, 1.96331555, 1.96331082, 1.9633061, 1.9633014,  # df 705-712
    1.96329671, 1.96329204, 1.96328737, 1.96328273, 1.96327809, 1.96327347, 1.96326885, 1.96326426,  # df 713-720
    1.96325967, 1.9632551, 1.96325054, 1.96324599, 1.96324146, 1.96323694, 1.96323243, 1.96322793,  # df 721-728
    1.96322345, 1.96321897, 1.96321451, 1.96321007, 1.96320563, 1.96320121, 1.9631968, 1.9631924,  # df 729-736
    1.96318801, 1.96318363, 1.96317927, 1.96317492, 1.96317058, 1.96316625, 1.96316193, 1.96315763,  # df 737-744
    1.96315333, 1.96314905, 1.96314478, 1.96314052, 1.96313627, 1.96313204, 1.96312781, 1.9631236,  # df 745-752
    1.9631194, 1.9631152, 1.96311102, 1.96310685, 1.9631027, 1.96309855, 1.96309441, 1.96309029,  # df 753-760
    1.96308617, 1.96308207, 1.96307798, 1.96307389, 1.96306982, 1.96306576, 1.96306171, 1.96305767,  # df 761-768
    1.96305364, 1.96304962, 1.96304561, 1.96304162, 1.96303763, 1.96303365, 1.96302968, 1.96302573,  # df 769-776
    1.96302178, 1.96301784, 1.96301392, 1.96301, 1.9630061, 1.9630022, 1.96299831, 1.96299444,  # df 777-784
    1.96299057, 1.96298672, 1.96298287, 1.96297903, 1.9629752, 1.96297139, 1.96296758, 1.96296378,  # df 785-792
    1.96295999, 1.96295621, 1.96295244, 1.96294868, 1.96294493, 1.96294119, 1.96293746, 1.96293374,  # df 793-800
    1.96293003, 1.96292632, 1.96292263, 1.96291894, 1.96291527, 1.9629116, 1.96290794, 1.96290429,  # df 801-808
    1.96290065, 1.96289702, 1.9628934, 1.96288979, 1.96288618, 1.96288259, 1.962879, 1.96287542,  # df 809-816
    1.96287185, 1.96286829, 1.96286474, 1.9628612, 1.96285767, 1.96285414, 1.96285062, 1.96284712,  # df 817-824
    1.96284362, 1.96284013, 1.96283664, 1.96283317, 1.9628297, 1.96282624, 1.96282279, 1.96281935,  # df 825-832
    1.96281592, 1.9628125, 1.96280908, 1.96280567, 1.96280227, 1.96279888, 1.9627955, 1.96279212,  # df 833-840
    1.96278875, 1.96278539, 1.96278204, 1.9627787, 1.96277536, 1.96277204, 1.96276872, 1.9627654,  # df 841-848
    1.9627621, 1.9627588, 1.96275551, 1.96275223, 1.96274896, 1.96274569, 1.96274244, 1.96273919,  # df 849-856
    1.96273594, 1.96273271, 1.96272948, 1.96272626, 1.96272305, 1.96271984, 1.96271664, 1.96271345,  # df 857-864
    1.96271027, 1.9627071, 1.96270393, 1.96270077, 1.96269761, 1.96269447, 1.96269133, 1.96268819,  # df 865-872
    1.96268507, 1.96268195, 1.96267884, 1.96267574, 1.96267264, 1.96266955, 1.96266647, 1.9626634,  # df 873-880
    1.96266033, 1.96265727, 1.96265421, 1.96265116, 1.96264812, 1.96264509, 1.96264206, 1.96263904,  # df 881-888
    1.96263603, 1.96263302, 1.96263003, 1.96262703, 1.96262405, 1.96262107, 1.96261809, 1.96261513,  # df 889-896
    1.96261217, 1.96260922, 1.96260627, 1.96260333, 1.9626004, 1.96259747, 1.96259455, 1.96259164,  # df 897-904
    1.96258873, 1.96258583, 1.96258293, 1.96258004, 1.96257716, 1.96257429, 1.96257142, 1.96256856,  # df 905-912
    1.9625657, 1.96256285, 1.96256001, 1.96255717, 1.96255434, 1.96255151, 1.96254869, 1.96254588,  # df 913-920
    1.96254307, 1.96254027, 1.96253748, 1.96253469, 1.9625319, 1.96252913, 1.96252636, 1.96252359,  # df 921-928
    1.96252083, 1.96251808, 1.96251533, 1.96251259, 1.96250986, 1.96250713, 1.9625044, 1.96250169,  # df 929-936
    1.96249898, 1.96249627, 1.96249357, 1.96249087, 1.96248819, 1.9624855, 1.96248283, 1.96248015,  # df 937-944
    1.96247749, 1.96247483, 1.96247217, 1.96246952, 1.96246688, 1.96246424, 1.96246161, 1.96245898,  # df 945-952
    1.96245636, 1.96245375, 1.96245114, 1.96244853, 1.96244593, 1.96244334, 1.96244075, 1.96243817,  # df 953-960
    1.96243559, 1.96243302, 1.96243045, 1.96242789, 1.96242533, 1.96242278, 1.96242023, 1.96241769,  # df 961-968
    1.96241516, 1.96241263, 1.9624101, 1.96240758, 1.96240507, 1.96240256, 1.96240006, 1.96239756,  # df 969-976
    1.96239506, 1.96239257, 1.96239009, 1.96238761, 1.96238514, 1.96238267, 1.96238021, 1.96237775,  # df 977-984
    1.96237529, 1.96237285, 1.9623704, 1.96236796, 1.96236553, 1.9623631, 1.96236068, 1.96235826,  # df 985-992
    1.96235584, 1.96235343, 1.96235103, 1.96234863, 1.96234624, 1.96234385, 1.96234146, 1.96233908,  # df 993-1000
)

#: Certified static table coverage (== len - 1 == 1000). NOT a cohort maximum.
STUDENT_T_MAX_DF = len(STUDENT_T_0975) - 1


class StudentTTableError(ValueError):
    """Raised when a required df is outside the certified static table domain.

    Fail-closed: the runner never substitutes a normal approximation, an
    interpolated value, or an on-the-fly numerically generated value; extending
    the certified domain is a reviewable generation ruling, not a silent
    fallback."""


def student_t_critical(df: int) -> float:
    """t(0.975, df) by DIRECT lookup from the committed static table, or fail
    closed. Never computes, interpolates, or falls back to a normal approximation.
    """
    if not isinstance(df, int) or df < 1:
        raise StudentTTableError(f"degrees of freedom must be a positive int, got {df!r}")
    if df > STUDENT_T_MAX_DF:
        raise StudentTTableError(
            f"df={df} is outside the certified Student-t table domain [1, {STUDENT_T_MAX_DF}]; "
            "no normal approximation, interpolation, or on-the-fly generation is permitted -- "
            "extending the certified domain requires an explicit operator/scientific ruling")
    return STUDENT_T_0975[df]


def _finite(x: float, name: str) -> float:
    """Reject NaN/Inf before it can reach a metric (G17)."""
    xf = float(x)
    if not math.isfinite(xf):
        raise ValueError(f"{name} is not finite: {x!r}")
    return xf


@dataclass(frozen=True)
class EvidenceIdentity:
    """The four frozen package identity strings the runner binds against.

    These are METADATA ONLY. The runner never opens, enumerates, or reads the
    real evidence package to obtain them; in synthetic certification they are
    supplied directly as strings."""
    package_id: str
    package_transport_digest: str
    source_production_sha: str
    evidence_schema_version: str

    def to_dict(self) -> dict[str, str]:
        return {
            "package_id": self.package_id,
            "package_transport_digest": self.package_transport_digest,
            "source_production_sha": self.source_production_sha,
            "evidence_schema_version": self.evidence_schema_version,
        }


@dataclass(frozen=True)
class Interval:
    """A two-sided 95% Student-t interval over independent cohort statistics."""
    n: int
    mean: float
    sample_sd: float
    se: float
    t_critical: float
    ci_low: float
    ci_high: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "mean": _finite(self.mean, "interval.mean"),
            "sample_sd": _finite(self.sample_sd, "interval.sample_sd"),
            "se": _finite(self.se, "interval.se"),
            "t_critical": _finite(self.t_critical, "interval.t_critical"),
            "ci_low": _finite(self.ci_low, "interval.ci_low"),
            "ci_high": _finite(self.ci_high, "interval.ci_high"),
        }


@dataclass(frozen=True)
class H1Result:
    status: H1Status
    cohort_count: int
    experiment_mean: Optional[float]
    interval: Optional[Interval]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "cohort_count": self.cohort_count,
            "experiment_mean_net_risk_adjusted_excess": (
                None if self.experiment_mean is None
                else _finite(self.experiment_mean, "h1.mean")),
            "interval": None if self.interval is None else self.interval.to_dict(),
        }


@dataclass(frozen=True)
class H2Result:
    status: H2Status
    valid_cohort_ic_count: int
    point_ic: Optional[float]
    reporting_interval: Optional[Interval]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "valid_cohort_ic_count": self.valid_cohort_ic_count,
            "point_ic": None if self.point_ic is None else _finite(self.point_ic, "h2.point_ic"),
            "reporting_interval": (
                None if self.reporting_interval is None
                else self.reporting_interval.to_dict()),
            "uncertainty_role": "REPORTING_ONLY",
        }


@dataclass(frozen=True)
class NoActionResult:
    status: NoActionStatus
    cohort_count: int
    mean_net_no_action_return: Optional[float]
    interval: Optional[Interval]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "cohort_count": self.cohort_count,
            "mean_net_no_action_return": (
                None if self.mean_net_no_action_return is None
                else _finite(self.mean_net_no_action_return, "no_action.mean")),
            "interval": None if self.interval is None else self.interval.to_dict(),
        }


@dataclass(frozen=True)
class PopulationSummary:
    base_population_rows: int
    scored_population_rows: int
    evaluated_signal_rows: int
    selected_cohort_dates: Tuple[str, ...]
    exclusion_counts: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_population_rows": self.base_population_rows,
            "scored_population_rows": self.scored_population_rows,
            "evaluated_signal_rows": self.evaluated_signal_rows,
            "selected_cohort_dates": list(self.selected_cohort_dates),
            "selected_cohort_count": len(self.selected_cohort_dates),
            "exclusion_counts": dict(sorted(self.exclusion_counts.items())),
        }


@dataclass(frozen=True)
class VS002Result:
    """The narrow, versioned, experimental_noncanonical VS-002 result.

    Self-sufficient provenance: it binds the preregistration identity + freeze
    digest and the frozen evidence package identity, so a reader can verify what
    scientific contract and which package a result claims to answer — without
    the result ever having opened that package."""
    schema_version: str
    schema_kind: str
    runner_id: str
    runner_version: str
    generated_at: str
    preregistration_id: str
    preregistration_freeze_digest: str
    preregistration_schema_version: str
    preregistration_verified: bool
    evidence_binding: EvidenceIdentity
    population: PopulationSummary
    h1: H1Result
    h2: H2Result
    no_action: NoActionResult
    criterion_outcome: CriterionOutcome
    skill_outcome: SkillOutcome
    observe_only: bool = True
    grants_authority: bool = False
    vs002_executed: bool = False

    def to_observations(self) -> dict[str, Any]:
        """Strict-JSON, authority-key-safe payload (embeddable in a canonical
        ExperimentResult.observations)."""
        return {
            "schema_version": self.schema_version,
            "schema_kind": self.schema_kind,
            "runner_id": self.runner_id,
            "runner_version": self.runner_version,
            "generated_at": self.generated_at,
            "preregistration": {
                "preregistration_id": self.preregistration_id,
                "preregistration_freeze_digest": self.preregistration_freeze_digest,
                "schema_version": self.preregistration_schema_version,
                "verified": self.preregistration_verified,
            },
            "evidence_binding": self.evidence_binding.to_dict(),
            "population": self.population.to_dict(),
            "h1": self.h1.to_dict(),
            "h2": self.h2.to_dict(),
            "no_action": self.no_action.to_dict(),
            "classification": {
                "criterion_outcome": self.criterion_outcome.value,
                "skill_outcome": self.skill_outcome.value,
            },
            "observe_only": self.observe_only,
            "grants_authority": self.grants_authority,
            "vs002_executed": self.vs002_executed,
        }
