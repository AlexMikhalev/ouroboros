"""Bundled instruction contracts, reachable payload references and original prose."""

import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
from types import SimpleNamespace

import pytest

from ouroboros.contracts.skill_manifest import parse_skill_manifest_text

REPO = Path(__file__).resolve().parents[1]
PAYLOADS = {
    "ui_design": ("SKILL.md", "references/craft.md", "references/browser.md",
                  "references/measure.js", "references/freeze.js"),
    "design_demo": ("SKILL.md", "references/brief.md", "assets/compare.html"),
}

# SHA-256 prefixes (64 bits), sorted and base85-packed: every normalized eight-word
# window of frontend-design_SKILL_marketplace.md. The test needs no external file
# or licensed prose. Normalize punctuation/case so cosmetic edits do not hide a copy.
_FRONTEND_FINGERPRINT_BYTES = base64.b85decode(
    b"09fQiF9$cx09t>6x52Et0D_>o)EC%Q0E~alOL#dn0Nk&e?C$8D0SkGGrWsVI0W5QZ&hOeJ0WK)R=tGFH0Y~_VWL_AV0jb+dy1E2n"
    b"0ouC8u%Y230r2<)+Xa>C0rr)Zum)G}0tVn+D^(p%0y()f>JI0v0&1IS_#x>u0)T*#X;+A#0~?3^G>E0n10`_~^pUI}16q|DPzq>q"
    b"1C)cY7D0^!1Je?&VNz=v1KxA8>cRFF1SWC~64N_^1V%OHhRsxe1bsn^iJ~=c1ojQ%uETtU1vR<N_LFI71vUr3BpykQ1#2(@r62VP"
    b"1%z&nV&2@l1%!IgAE&{s1)FL0Bf_tF1?m>+Dx)D=1{+QD@MN2w1};VS9jsEn20gM>g({CS22QpptSu8c2EYqk2N34b2JW~K6-$NY"
    b"2NWQooPA_g2X#Xm+xw7D2gy?^mNn6h2sRC(s#;LX2w-&MuO{iQ2xM191ux|A2-&GCSkQ4#2^}7}y@lmG2{5KH9gtV?2{_S{3Eryb"
    b"2|DPer*d#T32udh*<MUW3Lets+_Y543M75I#ZV>I3R2JXA~oiG3YnI_)0sK43ZM+nXv3NF3azFR?LGvq3bK32srGys3dy5RSD8n~"
    b"3i{tXYik{~3m$A_7dfuo3nw94uiSgN3!K`-XpQnB41Mn>?^j(+428R`cM88643mrX7x0LS4Dh&{R_44<4HK0vB2h(w4K8R5uTc0H"
    b"4K)}rji2Kq4K*yOe*6!T4adTeVQD1x4h8y>x_2i;4in&lPPGP%4yB(p{06ua4!OZR;wgT84-L?4g5kaX4_AbIO``i)4|^yr&<nsB"
    b"58D(FuA$tY5AoItYXM^!5Aq-|JC+SV5C#7bErm2K5D$TWB)D@w5a9X)a3i2`5gBdlLoAfR5l;|$Q)^fm5rVos7k%=95sL1L;ILt="
    b"5xP5RPnkZ|5x+}_ICc635)~85&-FAB5<U<7Tu)2G5?j!Dbq<x}5_SUcqCECz5{cP*4m~eK634;DH&FJc68bW~D9~%)6G23Y9T7xE"
    b"6Kb)b%4HOq6N@tLax+fp6YKf=Kx~vS6c<5|IgBP>6dAQ=d)@076eY@ks0^p?6ek|d7{t+h6pJpIWWOwu6rE0>X7rRK6t3j4M!+l)"
    b"6yVpyNY7`m6#SsCoFrx96%)esX9B->6)<u(mC8Z36*yD{AFn1s6=~S}bE*~^6@tKQI*e|&6|SLBqqK|?74XK@BWF8~74(fI>GX#2"
    b"7A?VUMT>+G7A@lts1NEP7B>`rao9N<7ED`(=8K=Y7IgxpB%YA67I!*JYOeN~7RE*7U<+hu7S+AFi_yTL7UNRn)1e#$7go-qq_U3T"
    b"7hBM=<Qr2=7lUEpe`Hqn7l`+^$V8M77oqoX9(F;Q7rNPq^a_7i7u*yVn8~L_7xhFT{fLyo7(xm((nw}~7*2eRbSG+t7+K<HbpndJ"
    b"7+ke@^YRc!7-CKBMwLIK7=mvOuX6TY7|89*#OeHK7|X<5T9u`n7~XQp>gv-R80Mb|-HNQp8Ix?}Ir?RQ8JjAgxke*u8MA6tkGgms"
    b"8Nsp{1-x)M8Rrmr;V!6Y8Rs`cZ}#$88R;Llj+Csn8Y%>m`x#uN8Zu-?qGtRD8iT2viHn{f8i<$7;t@D#8pXrrk#l_q8+NQExA3kT"
    b"8>wS~i3^j58|RnTGf<}p8~AFEr;1F`90YYJ6noF9930%9iXRvt93mlNcUC?P94q0v0oxi295tH?-w1uW98KXq+Axze9AQOj`+g;k"
    b"9CBp1Gw|lo9CHnF#-1&09NPkKaz`ZV9Oh_CEc6V}9UdiRGX}Qn9V^O?*}*<y9X%KKp>)nL9X%b)Xw;MD9Z4!I@K<rk9oM)d+8Vgb"
    b"9sTBIWhvK39wmR8l$9+Z9xuUVt;8Jo9x!LxtDRIQ9#JS;(qN|!9%g$sQjd6U9<0FyVX$D{9>!+R*3g1h9_=TP(3L&d9{4f>(_emQ"
    b"9}z|6r+9O+A4fMbm(qgwA8*-V-lHf?ABPnhlI$q|ACx8|gnL0fAGjkc^~D2PAH7Ip2ZHptAHo7?_jaDTAJ#-ml}}#eALxtZESlA&"
    b"AP7X1*%vk4Aa^_tbl%ETAd(>&hFKk|An%yE)x-QAApG_cq35XQAtbbL%aKPTAwGvB2<xo^A!4Hk=^xaVA(NTHCw4grB5dsc@6BFn"
    b"B8?7Mr2RjkBGyf;dwdNgBIQ-iGd>a8BI@Z?B^FIYBJWy#-o3}JBKvw1I2$3gBRpD>UCy{qBXhY%m-Z0DBmu+xX9_3VBm~WbehJ;;"
    b"BulEmoltAiBwN=lTRw*bB#+|u>uCB4B-SauUzxe-B;3YEep?|`B@`-VU$EEUC0o{!G1xFXC0}3FnTb#RC7o*U?;oohCBbx{Y7|~;"
    b"CBu%=i1M<3CBw5qo#-YvCc5j6-IUMWCfn#?rVclGCh!levxXsNCjKwDurpBlCklw!`VZn_Cqq-!3a-YkCr;ilIfOD_CsIf=e?{+Z"
    b"Cs>3GTKhb2Ctqr&3i;D_Cw~)9R7IYwC!r(gchIc<C#Y6F#D!XDC@VpVm!_FoD1>#^DEZf?D4VbXjTP7=D9<_68uinLDKbjoqR0em"
    b"DMtHP^TigQDQIAE+X!sBDXlhIf2>dMDcy%BsEg()De#nw<K4f~Dka{Xu-csFDlj~R3<H7HDob@)bdN()DqC;13(jdrDuEFU#gJIp"
    b"D>D}SI#tItD~gZ{y&m&DE1dKr!MLgRE29XPTDf^fE2j@<{j#=KEEEv^cO%2xEGz~TUxMp}EL`P_y7wM5EOo;*d7lqIEZgetv2Inw"
    b"Ej(AI{B=(REk}s5>x14LE>2I7bEu4zE}(|oCF;u_FAl6xb-n(PFB;XW3jf}jFEh9ru_9bqFFbws?BsDeFG{CmapB7VFIEfi7;fwQ"
    b"FJp-Q@5SukFQ*?F9M+q&FTJN?b0(OwFV&Gq8v}hZFXq5!5?;WHFi@GzG<npNFmB0&QFPR|Fz@V&G*r?(F)dlMb}d5aF+cFGgm4m{"
    b"F;yhje2Sm1F?vRp^af!PF@(jJO-p)SF^;J=MoU#;F`m%(%p*}7F{AvRiAFO+F}6pulpN_AG2SM`{AuG0GFLEB`QJ|0GH%6RN43n}"
    b"GIZZpFvgiMGM3Bt66(m(GNv%Fy3gGSGOlRGr8-4zGOqz3biDt<GVeKI2D3PmGX{T~tn*WSGY4D&o+6BcGc>9jdd$<QGdA>>rosKt"
    b"GiBr*6#sjsGp`4mwS>hLG!%DNaQu~oH4j(!7h(KQH5^Hd;HiZ&HEdZPO)kzhHF39f1J4nBHKD#fZEjSIHTJ~ExFoaBHdq<XgF*y%"
    b"Hiqc--Bw-@Hjyb5M>`BWHlI>V_tTESHll1N>lnv7H*|kVd+(z9H*}HM&b5&VH+y-V)O&}hH-dkbo&rpEH@g_Rn982*I0lvs?NUNQ"
    b"I17a}d+CV(I43Z+T{Q7dI5zvqyBT!uI94=}F3b<=IB+;ZtpKGJIFC?5U)i!;ISm^W2|8OQIi(o9geR#$ItohYar!)MIxp!*rOpfh"
    b"Izo44Pxi?NI!$B|0D$AjI^aH7^X$o`I}II-#d82xI~Z1o<*6peJ1C9C5(!c<J2EAYrok-IJ2{qeF4(!1J55d&v~TjLJ8r{h-@^Lu"
    b"JAw@02&7D)JFw#$8GOMwJST=PJk@7CJUNI<2p`95JVp_7HgU6yJX-m}eM{uwJa^^brMcv{JcojO-UGL8Jd!UTWA%5$Jj$l*pH7v<"
    b"Jm&_dW7_lIJoV2@OFu%)JrDI(4#xn@Jt-W4RK&|ZJuI47)*h(SJy;DKPf<WeJ#tn5t8g%aK4(~8=Ufh|KFl%P_|SeqKLVEXeapGk"
    b"KORaMjhhQNKO_gtz>_(kKTyvyiGztuKV+5Fy)T5tKe$Xu)+=$KKlvf)3{}PeKmZgtE$wa9Kn1jeP`uRXKt7kR!!^keKwH^6K+YX<"
    b"K&kqc7d975K=Ut$G!BN^L1HuoQbAXiL7Jaj?y?vOL9s~LEgQnQLFbV(Bga<OLH7_?2883?LM&)!4}Sl-LTe6(<5GcJLUjjSJNbrx"
    b"LUovC4%PGbLoZ7rz)-<oLxO#%m%-icLxj*_!0}>9Lx`OU$$*3uL%6BJ2<iqLL&~qRtd~}qL^axE-<YXtM2pLr10VQOM3{rZ)3N-*"
    b"M9t=N8XDhpMBW0<WOhXfME;((TDi7wMMc~+d@@)%MNrj~F4gMRMNzE#7TF99MU?17@XSE>MVHC8<m--0MWauiEN=nvMh5<ZCEM(p"
    b"MiAITwj-U3MjHX#@(99`Mj+is#LZmVMnvQ*mG<qWMy$%MNE};2MzxT&tPG#HMz}V!_^X0kM*7kCV21LmM{+74G5z+?N2s}m3lWr8"
    b"N57HpS?hjfN5+e&QwQ3HN6Dt%*@msTN7>aesX!m}N9h-}u?aW1ND5wl)qKcbNDTA#2gjAKNLwqW<I|BGNUG4pzrObDNV_36{hULd"
    b"NjO9^ia^o=NjPPw?zeT(Nn4dpCW}{LNtMUHA(AWVNxVI>Qu73uN#sf+y$yu3N<%q3d?)LyN=wkHx}k&zO3-T5$CvxEO9AMI{$>z<"
    b"OAudfTR3R2OO8(Lm6y+`OO$?6!QIs4OPpg%G&=b$ORx^TO$e>HOc6$j$9zkkOxg#KRE6(nO#Vc0w#s%{O#u79VSiQ4O<BjUodjTM"
    b"O@Qy*22vQsP2{ZZqI2ogP3bN%zHZ^XP8(`~|C$RJPA0w*?ONBsPAG;2_vmfNPB7lZndri;PLZEgn(imLPLa)UBbeQ#PQ?!xEoz>U"
    b"PX8{~O~tO0PZQTDnPa^_PaKlFUVa(NPhZQL$NzbAPh%?q7YfS?Pk4tGiqph@Pn8>(#(CXhPo_&S*?*adPqUbwmGnbqP%^z-Z&A#&"
    b"P*SQ`F-o3WP+YR1@TqlPP`}`WQINHCQ05rmx$bzOQ1S9XT}-t+Q3UQQc@5!%Q9?l$tKOwCQJkp*Sqb3VQN{L}1%t;HQU!5IzLo0T"
    b"Qdi0@JT6{=QgTP6We#v*QhW`PARheQQj!A+mSO>7QlSJM9Qq<`QpYHc0DiG%QzxHc`KQ&gQ!I0mD`JwCQ!PwYbKXVLQ#*5$L1Y^Q"
    b"Q$u+wUeMZOQ%>cheE{7AQ}iSaPV4n*R8upL>~(@aR9+nT(jk4%RB${iJ*At7RCN0zG}t!DRFv40UE5~ERGnS10-r>(RLVHt0c!#l"
    b"RN^k)xwZq2ROwO|8vnxvRPMXP+V3QXRRLpakltwdRTj6!3(QrnRYLu)Vrxo&RdA}I>OXziRq3H(UoAx6Rqvj&`NaA>Rqz6=ub=N("
    b"Rt?dep#y}XRu{K86g50;RyX)xIqk<>Rza!z(Z#g&R%JHrAhKz3R)o;|oI6QeR_eAXOy6ldS4JtQTk@$>SDXr}lv_J;SFiJa0s=_<"
    b"SJuBg_+mDSSJ>lTVGTtASKcq8eKiNiSK&uQJXY4ESV=O8Yh)0_Sbo;9>n_CdScSPAdf>tdSj+HNSX-(jSqpPD_xg*ySsT4HH#r?D"
    b"Ssu9n&P~CtS#rD@dK6@ZS$kAYl;N5SS%DAVnrCjZS{UW8G{x!bT0hcuB_0eAT1K&l5sq>LT3<v@Q{EPBT4o8qFdGoSTBeK1lT55>"
    b"TD-L%*>m$;TSt+qvM%%(TarA26qcIITbxnx=LE#dTg%-Q$u|V!Tk>-1&c##eTm%bqf2(L6Tn>f+kKbUdTwR#4mw1cHTxfj#sZ`9z"
    b"Tycv7uf~1BTz}c3{sO>CT)R!y0&p+(T@;Tl=$8gpT`G#58(3-mU23-y&>j5yU7!K8*XojnUTn_w!D5}FUUi{xO(|{DUapI#G$X`c"
    b"UgcA_8;=8qUm^ZYq!~*8U!i6-A=nU=U?&G*@2v8}U_`neCtIBKU{-QjgW<a;V1W4CZ+9NWV22gZ#4(X_VC#$z(7FkEVJpe(@?w`|"
    b"VR>YXBIsk^VXQ52fJrA{VaBkK;<L2DVdcYq&nBA|Vgh_g_wtWYVllo}&{@cEVn^WHyzf#CVr(u@8Qgv3VsEf!G8c&sV)EN9Nf4k3"
    b"V)RR0JwZ>hV-uaF?1dc$V;tXE+@#dJV=2QiWHPL5V}gqod+yb2W04XmNfcC)W1X|j<WX=^W5QeF>I%5nWBSON;)jEIWDtGc{@NA$"
    b"WD{RA4b!o|WPVxq2~4|cWSY$sCx7#^WYz};pI)fkWcxW089>U|WdxhotfLrtWieyejjLCqWjL%eb%3n%WkXx9e0hXDWnH3DB2Qj?"
    b"WtI0}R*|skWtz?s*JgldWuw&5|Cr`sWy(3jYCKWgW#NP60pWjEX8g_~-+$r%XA|P!KkP|3XBK9<mJ5x)XCT4r$lycwXFUxE5n&_3"
    b"XNCw`0^vvVXPGl}>{GfmXPI_K{3S;)XT?Bg<to~7XVZQu>?)g_XVrHEdAU&pXa4;nS>IprXgT%*wlL7UXhGDQXmAjSXk<HpG?enU"
    b"XlAj$#)m*;XpjQ@zlYL^XrY;h)m|M0XseC8qLCj)Xv|p~y!D5)Xxxk2;#KcOXz1f$sZ?_HXz==AN<be2X!P^HE&u=9X#q_!m=He8"
    b"X=bOUVi>gYX^Z1T?$Pp%X^^?62DgETX{6Q|EfU0DY0*%;sz4b=Y1Ux<2tj2YY1bAQJEeIiY7?nqne9XGYDYU<h`pY4YG6p-^7pa!"
    b"YL=_?dT%z0YN)fW5b238YT8qSRrf1eYn4);HP>VlYoBP@OaPM5Yq5sZKQJiAYz3Nr3?$v(Y%1&fHHg?YY)gQ$J;5JKY)m(jIk4>^"
    b"Y-aXNY!4d$Y?>SI=$>^VY`V@+H`F4wY|fk1)`v`8Z2|Bvu}U?FZ7Ht_Axs&%ZD2+D>@(R1ZE1MxBw(xIZU^zFg@tYWZc`@%816BO"
    b"ZhqYa6sCT@Zj7<kLi-{ZZrF2Hk%;l>Zvvh`+sXE$Z+X`yxi4<pZ-aiuu6znhZ=1GwjupflZ=gIudEV7`Z=*irnyg?$Z^i8^G4xAx"
    b"Z^)9KrhzGAZ|B5P?02`Ta0%6+zKj0wa6Z|?JE7jPaDN%9ZaM(waPmuw&vnn(aS~a(A9wywacheiLRBmjah-7_1sD6^amEP1SUi8y"
    b"auVa%<_44va;k>dhQ=~wa<S%?<TXY*a?G}me};l0a_wt8L1Y0_a~Q;|>mF(yb7OAhBhzN<bDwvpSqL~*bD^0LFlhaSbJE$!$%95t"
    b"bJVc8ZM71cbKF?fj;$r^bM8?38RZ{0bV|c0|EkM*bkmB!WqM;ebno2yydcCTbrIsl+u<Tlbu%?4>cd|(bv1R*i-N%gbzSRAtHM5b"
    b"bz=U-cn7O=b?M~vY>?)uc0V>hPJ_UEc2_R1qB#xJc4AF6_as^zcNdzjD*IMscR5exGKb7?cU+W8k@@YZcZil8JPpM1cdVS{ON?lA"
    b"ceqYx6WL?GcsebeC>?$Gct|((ogUc?cuJSm6lN=ZcyG&Ss$D>8cyksH4y#>kc&#Hb-Jwc@c>H-5#=z3jc@beqB9~Rlc^`+<BW>>U"
    b"c}HMEprK^xd5hX`W81KodKV3*6qbe~dKe9>bN{lhdRjQvei+bndWCi3cGPi|dY53+y^<wUdhYn*O6dK4dkJztwkEoFdoWpA8oZeK"
    b"ds_mYv&S3LdtYg$al6R=dy)1#8DJ5Wd!vF6UbY=ed)1DX6uP0+d)!$-UG~lrd+3GILECfPd-bsOP(SRGd=BC6-|j1)d?!WljF}5T"
    b"d@^i1$?DU`d{g@d+%(P$eC+AT){uz=eEc}7t27F#eH^Qt$KLBZeI(BtSRp$deOwh<gchgSeWl36LAwtteZ+t#r80!6ecK&EJt{jo"
    b"ekI#FqPECResMaOFHiv3euYQ)bbpt(exnl`Lvcgke$=eUX9*NQe%w`q+s8pHe&kCM-EG_Ee@p{3wQ;G)f0)?l=DJVgfDFngWnns{"
    b"fKgueXcSDFfLmq9W0@AzfMfw;d2A_;fO(w@tR10LfblI;&yh~!fcn6|GuoBefh7-fd+z&Vfo@WG^9uElfr}6O*VYdPfu0-z&zsy!"
    b"fwE#HB*V3*f;p(1unch=f=O@9NA*m0f?4~LZ>D?yf`YfyDdS_Bf|W{xo|Smhf|p;P{#U-7f|yKuj|Mu@f}k3{{L%TXg3b;85Z3ZV"
    b"g9NW)m<suNgAnAaO+FjIgA<dlNz6CXgY7Tef|RN~gbE5kcHEaTgeqX+rmF}Lgg3;mqLdLdgi|Xs_)}a%gtwZqq8j_6gwnAl0oNs+"
    b"g!z@w_9HKMg$JgF(3Y5Fg$iCVGMdnIg&JiKIU#Kgg&%bUb?7t<g)I2GYox<lg)jU@8z>)Ug*VB&p1kPmg+r1N+$XEeg;~EDw&SgP"
    b"g<BcqPL*nvh0PsdKWz)fh18JlKtFhfh3Ya4zc{s9h3zi>z;n`xh4;|R6}CXUh59;%*Fn1ch7035lrdT_hC7Uv#~Qu=hDbw}NGlIa"
    b"hH07Ao&bB)hH3ng5T3wEhJ$T}W;y37hNHqXl!O`QhNtz<R%{j!hOGBL>b(GnhP@{%CG7bVhRmUF-M(IuhVAwxU&E~whVK+rGBm!`"
    b"hbIU0##%<|hm#7%ZV+-&hzjPn%Yo(Uh<s8BOj!E5h>)ySV%e_Jh~jD3?W&IOi1lylcbPLBi7hyDqq-{vi9LcK55F4)iA+iqE-6Zx"
    b"iJ9bCj=L@RiUO@j@ATlViUpyf^b8eZic>!meW@NgighV)s=s5Rig?-eojBb}ihErh(U12xij;6%^?l+5il<f9lx(p4imRKPugYA?"
    b"iwZ_Ef!S5+izq@rDhNrai#*p8enMMKi$vIJGftz7i*esbm^Sm`i-&%OP+$P$i^+67$3;_jj0AD|d5Cz^j3g!%etYS2j5R&&w64T>"
    b"jA3A;P&_5-jD9_KW5_a3jH&$w_7`lwjL(!@8qSu;jORMGMrQ#^jT18-J?qZ)jV%a-wobXijXNYDThx9@jYsoX5nmMLjp0PET(DtG"
    b"jp`}YLYUybjr&HJpT#wcjw{}DP@;;*jz@QNh<iuAj$s0$h+kjVj$vtt1V?vLj+uoC0+5I2j_ijzTL1fDj{^ANUUS6(j~cTe)QSt%"
    b"k1I809x8%tk3XT=MO)8*k4J-ph$22Pk8&7~)`?2Yk9=iN;Y2ltkAnB-zh_STkBFG$(tk6MkD-~%sFo?MkFlLO>EJ*`kKG?MH58RJ"
    b"kMsGaUen&vkVbAPpfzP9kaT4DFycL9kvQDYb(+=LkwM2_PWSJxkw&F&sA=3dky7Ut+L2JNky&qjnS<Pkk$QUN;x1oHk*9;RQ0Rip"
    b"k;p~ZMcb8~k=mG;9`T#!l3Iw{*<gq0l5~?O@XQ;Ol6LkNW!ju5l9DO9?<ZMDl9K#2#HxWGlF1;rXOTbplFPZAX*^rtlI2}GNR%=w"
    b"lI;hamR_%SlOm}Jd&s5klPs)qRwXzllQ0*;tVgGvljVd1<?CU$lt-Wx=PiO#lv51&yMf0pl$Zwf+T=i<l@iDETG>z2l_RWu?M0)Y"
    b"l{+M>EF<3Om0W_J;V&7Wm1r`1nbUsMm2$$PH{-MIm8*}aMj*MwmAryt9+m`JmF#BtB15SOmGig<52_cUmH(N|86x)^mN~=h1&ZRs"
    b"mWVb$*9O+}maz@KqmME4mc1ueo-dgEmcUrE1UyE3mhRSX*@X<smh!V39Q!j6miK$j-fp&|mtxO8#j$b9mv#<>lxMr-m!gX?aJ7Mw"
    b"m!=?o^U-t%m!`?c3{Rd$m#5k^I@?^Nm+ak8b0C1Pm-1F1!Cx0<m-f?pA$-w6m@Ec-7v=Z8m^t<&3O75tnD8|u*CDIZnF6MZHf*rD"
    b"nH-4!Op7-^nSOj;R009<nShoEkILy6nUg2wAx(OJnWT1+sKElcnfkqBtncTxnhG<$?=={fnu;xV(?D3=n$yq@=Z!B_n%x(1lZA;6"
    b"n<QeRp58?sn<>h2_z8tSo0-B3rX?QAo1f3+9D(2`oF|6BuE6{`oGBb-;y115oMq0?xRY2)oQushM4u?=obV@V&7Ef^ogIsFdLLL1"
    b"ouJ-I2>1!8ouk+#5uotFovRox-Apv(oysXYo_wQxoz-&lWyxGwo!VgYt|Ymzo%ijcpyJ^+o*CEF+C$J~o_8cFmZaILo_Ein*!Qog"
    b"o_frA@+`!wo{=JE@sJBwo|g&jj}}?RpJ1z2;2HNapM^@fcuI(GpON5257rGupPKS1=e5FtpXu3o>`9dqpboL2c>yGnpkqJHs!%$P"
    b"pmj*?kU_d>puHuQOnfrZp$E)hvbTT>p<Iz^a2T;Np<Ju;-?B-?p|<LmjMi1oq0sBOFZ7XLq48z>M|$4pq5uwxJ1Vs{q8E*c4)Dgu"
    b"qHFKgG#u_HqJ>E-gQ@1gqPbLa$2MKpqZ`{r!_Wryqv}+regPNaqzd`1Rf8<xq$l<A(O`s=q=0huY7#>#q?*Y5N?C*xq@c!~rl4d5"
    b"q|D38!P=Q?r84a{yFwU*r8lW7(FWJ<rEb2Z5jIBPrFntI4;|d(rO6_XAUJ#rrO|t9(eV=LrQ-J;3k){LrTWJ?5G}?6rVbf=F@LTE"
    b"rYt)~IO)H$rdYKBZKf>9reSJW;s*hwrh4cHGjN$!rk(Y7$s#-Sr$$fWQyaO6r)BU%WVlY_r=UqmRss1asGy^RBny-SsLgYUR=8dy"
    b"sPr&{?8QLPsYU3#ZSNEwsY$|;^qJJ>scEn7xP@KUsl-bWb*P^$sq6>L7-c?^srII$lK59Tsx;Ev9rEJBs#~U#Cq&rss+~Q2TgvLg"
    b"s>BE`IciwIs_Ge0EVzBvs_LbJcSq;;t1I*<173$Lt4Ox8GRb5Gt6n+=;@KkWt8%N1N8?oatJde-h~pqztJ|<W=H{u|taw1qlNY|g"
    b"tb1~5E^x%jts&eO$9P{Mt!Y@k<e(snt(aI6sWRcvt)uk^t^!cOt<q^6o}dytt`(sDh~?JOt|_drZ<Fn3t~V)Wf{1q5u0aW{S`H=%"
    b"uDzWg!ppCauYs#<n1|lSuZaC{NtBTEum07N@b*GXut8E|LRJknuvHuEJ#jwzuzlp$8qa8+uzv<7@4>_du);~L!K<w{u;FPfR?lnc"
    b"u=#56$u)S-u`tj91v9F-v4e!eN%7-ov8YPE`<HFZv9uIo^YYcLvA7oxF&D;=vBP`&1_+M`vCT}s=8M+WvD$)}oon{rvFYwkZgWRG"
    b"vIDCb=E=0svTvsFv5GMYvUTji>VW1+vYf@j649V+vkpP<EL}*FvlJ8{YsA14voY80S{$kzvqO`#<g@LFvrx5N7Eub(vs#-D1sKX~"
    b"v(0P28@7cIv(`}+{K-{zv*=y{Z;VF9v-rOelQjzYv=2M>b2WFJv>%&l6I4K1v@D<Y`^0$Tv@k4-?k_R@v_3t_qK@aSv|;v=O8KD;"
    b"v~MY39M+dmw0fhiw8DNaw6`2DP2R_0wBgr@=4oxtwF8Y1#jkhAwFx<F{C2_CwM|>>U@`m5wOQ&9NP%idwQwp@5<GH3wT(VFKn3Z#"
    b"wU;sAsK7REwfu)GZ|SSuwh4iOFx`iiwhDtwJW;pjwxROF?UTTGwz|VH&nj#iw#WSyG)0Psw(7MWHLm^Fw;P<Ojt^D=w<bJ&qP(eQ"
    b"w?(Pfbj^X<w^=^caLCzdw|uX($TDKhx5KOThytv+xAM9)-kP3OxCk;tak}h`xJEJGj^Pb}xQU7Pp+$~mxV?q5g&F;Yxs+tU-7dYE"
    b"xxU}>FHKV1x&9aU$$LD!x^}d;%SKW@x`1aHuf5!2x`|I2v?ISsx{>mr0taHBy0?gOUY`u*y2<O)Um4vOyL)wZQn-y%yN%<?a{CzL"
    b"yPBt}_R4SByR~SQ8_Nu$yU9`bHH0tPyWLlsc;X&_yaX!F;QWVPyc(c+V&Fkwyg$8Jk6ud~yjLAvDyMe;yjzn;@^v@0yl);ZD+t|_"
    b"ysAB;EDqTiyvX>gxN$$vywOY6_;NVVyxrW0sN4Wey#<74Q64Y#y+Nti&{5*?y-9!DC<6G3y;n*qpE)YRy>8?@8ij3<y?G{eq@VcC"
    b"y@}LJD3n$7y^g*@k^#{Yy`Yb;#1&8&z0c@13ob<Az0|~lsx#XGzh|)qed_fWzzuEOK?(xjz*mZu$_o@<z>$(LH8@dbz?<y?Lf)X6"
    b"z_6=tG)7hLz`~14$H)-5z~CW7IXcob!1`GLMLgBH!1`Uw@<5EV!5i}=RI1h^!9e-v$WH0L!9+776x8ws!BD*_h)B8$!E&<BGa@^X"
    b"!G)_BrJa?7!O7}wA+NL;!X&JF0qCJ5!b?uwYD6)2!kg>;wpb`Z!k=&pPlyo_!mbn<2SJCv!mf9!X>5pK!st<jD#~r0!vyrVUI(j$"
    b"!!WP91Ts2|!>25(qD&q*!>GA)fJXEV!^!<4!TFt2!|nFvpDZ?D#2sYZN`MFV#3BiKqgV{r#K!B&#szs`#Ys)_87x0q#cPPikH~J)"
    b"#gTfzxJ)y?#pP{<*Op+W#yz-jany$n#y)X-n4ZGF#-&kp%(N&^#@%1E&CN>$#_4|sIib)2#`U^1y=Uh4$6oLFSr2eU$9;4SGRj2="
    b"$EGv+6CwmX$EX+T^^HQ9$E<S3^`7OD$Ilg@%*i%T$I`-6?IxkP$NDz#QNu&A$N;WIU!(V<$PAj1LfP`2$UG4CqB#OI$WDXmq<{~s"
    b"$cRrzjfXR<$e|##pTInR$e||yD+W7R$lDVQ3KJvV$$01Ym~a}_$%{w0(OJ^y$)|4`O4rH7$;q2Vg{J1-$=T7JhjRq>$=?S81?EGk"
    b"$>qFWgz@Te$?CF0U^dx($^Y71Z!TQQ%0Cn>`lfIG%8D2gG?oX6%Dk(^3qL*+%K>@w3^q2!%LPiQ*Z?Hd%QUBwLz*_E%amQzUc)yP"
    b"%zg1^{RNiF%(q!WS~rKr%)Ft+j-&nV%;xc@O;ear%<xwCEE(@d%{*+CQ`o>P&0VNUPG_u2&9Gp>53tPC&G2@RLkwVZ&Kdv~Vthi?"
    b"&M^*utAX)I&PGrO??i<h&Q<{Q9j!}l&bv_pv?QT8&jvgQr1~ax&!TI}tL2NT&!cq$Wz(ZK&({1`Kk>?$&+-G^T!Ezf(2sQ^ju-2l"
    b"(3`e*ki0Oc(88i0^EwB^(97j@R>gdp(Di@lX22OO(J^q-B7V?^(S|Uleu_JI(bPJf*+%5o(fd*XBD!Eq(i&;zhfNfE(!$SUP^~D!"
    b"(*K1{4u}mm(*^5d8tTfB(<E9;O8C9S(>cS}g-7>B(`6k&2TA&k)2bzwOJqus)7@QD?4fi^)9L2b6GMd4)GvfFeQ>3K)G#{6zf?ly"
    b")I=o2%M5gK)Tef@8%J|w)T)bn4p`L!)WuIGG@H@7)YBy1#HzE{)YbS7o+K>T)hwo_s&NA6)mZw5{g|eS)r;aiT_GDb)&kOVN(b{y"
    b")*qmDe4|%b)>_paMy#dA)^kgU%{u+t)_eZjQ*-H;*2*Ozuj@v_*Ckb!O)c9A*Eom-M1mM?*QjlJ%!osy*RS+Gj)M>u*da|#7b^!B"
    b"*iMD^I|yo}*iUE!#!-1h*mAP;*Pv~%*reX)D8ZN%*v}YTWn3<**(H>wG!?SV*-A108>s%y*^x;{uLY#b+5c=r<l<l++P0S0A%Y6H"
    b"+RA)T<pBJh+RaE)1P0vM+abEC-R+~S+c-Y4R$#}!+eb%~Y4WiH+oZP(Zl|*a+rcfi($8w!+u5UJGKEGv+xqQvmi%Mr+zV#{deoB~"
    b"+&t+*U=3?E+(cgX(d9(A+;7=}?qeZo+<@VX6cez$+?^2#bPoaX+`Cf}1X5c+-0QlyY&>nn-0fk=Z1hcj-4Jk)CL)~x-5WIzgGmn`"
    b"-857ib^J8^-X<rq!@%=f-a%U6|1e7M-fImr*{G69-hiPOtygnr-kygwbSL3=-si=T)G>7E-t<XR$DcGX-ukry(YC<o-#yY~06rlX"
    b"-&voh<7e_Y-*X38C|Gjd-+!egN?;O^-;_EJ(apA+->7}sV~#Bn-_Z-OiuB!D;1DztZ@X~d;1H@UZ=OCB;6);fTn(9z;BOQ@<y%x}"
    b";Hw_alC}*@;JVLJXQjM};K)$EHb9pM;OF|}1BC;D;P@&_ScK5?;S;zGCpvSo;V&hcrQmx@;W2O|&nMp3;XV{aSf&_m;Yug^`DRQ;"
    b";aVZYg-me0;cL^P|DTi^;h#RiH+$(F;l9;ezk~DR;mCh4%MlI3;oqbqx>r<(;tdZ(J;D~2;v%(<1r?=`;y?P;jB%(l;<|u(Gt7?-"
    b";?FQ&wQ&z@;`&AhH8HYh;|{}3(ynAK;}`>@O7RE-<3aCWm5VHQ<3rfj7x-);<8Y$&7u#6{<95Ow8uS1}<AFt>^_jM^<C6L|-iOX`"
    b"<C^}gLH>QZ<DQIhS_oie<IoLEGMH2z<Ngj=`x?~~<RRTPtlhZ1<WLFClj$r><XvC5VZyCr<Z;LMitXPq<dPq6(SV7Z<f%nPLPBWy"
    b"<gH?E=EoEZ<utD6G?E25<y9HOUNgjo<#X1bHNa*m<>U<IIKVXM=2r_HEmuQQ=E~x|NDlaj=VYIkWgdku=ZT6ep_^B&=oZW|G4|sL"
    b"=qTdj9bHYX=sp8avFXW7=uieH5Y3o>=w$m&2qZF7=?~2s_#I<4>9%&EPR@V5>9&*guhGb=>J{T7k*tIN>QK0UNKR5B>Q|k;Jzz=s"
    b">R32n){*V&>S>Mh4tB}Y>X01|&UW%%>XX&g%+&8f>Y-?mpp33T>aj7PPT~$&>f_u_Y!EMB>l73o8I9m3>m@)mHPlpN>u=I6c)~e-"
    b">#V0sd(w99>)hu^{~U3S>_f&;dSdTr?1{{w`WWQO?5)^|Dn`I;?8WRg0Sars?FpW<Fv=8P?K2T?r8{w;?VDwbwAW^=?V_h2SD|mQ"
    b"?c6=D%=d7_?vK%VKB**r?x(MjgaD>t?+H)TeJ!FM?-Ov3JCrYP?|Klh==jo>@18w<bqfD~@3pdYc6Ijf@41zV%YRxI@4uD>##H!q"
    b"@Gg6_{~M{<@M2+-BY?r$@Mp;f2oc02@P6ht$}h}I@S|_&{|&R3@Xm&LK{<En@Zk+8aB%Ov@aD(;6^-v|@dyFUDHq|{@eh57CU&OV"
    b"@l?w)nioHG@mr!bVzktf@orQ<q7YLZ@wFvvZs#Ld@!u;ENb!xH@$X#+AKlds@%l1@;`NJn@&<rv7I3uq@(IQi*L-?@@)?#@hKT`D"
    b"@*m7j2$R@7@>}h3%yFD?@^)Kt8#$fd@^-QKH&KNK@~|Wi4X&Dh^4d&8FPM1;^AYcoKrPCO^H5S3ENK?y^PObM)067N^Pzn^Xx>Hv"
    b"^R4x@CV}?d^RUV7`)-zk^U{GfJYMc8^WdqyXHwF@^gzwSQ?K)e^j1u?*4d76^kJHW`B|=6^lvxO%8!^4^rK0Mj7Jlu^s?e=fKHVP"
    b"^$dQ1%jaFK^%hphl7&M&^)XkGgRGBP^<rW;ZCbAY^=2BGi0A~a^>3YyaxoX<^~PEHH?dgQ_1*MH{&dHm_2F;?*2~DE_9MgFz{z$R"
    b"_B_$)5LT`z_Jx9G-qen)_L%n}O-P|{_Q@nq3D^k^_T3QGT#3YX_UQ_oSUW-s_Xtu$;K1CZ_alifqICz__b&w|^1P<>_ctMMggw?T"
    b"_ht1Rb+qBc_t%{6%XKJi_t(_07}Tlw_x2G-=?uWI_x$;+!oE5O_zOt%{BHgY_)f8`7}6^8_*EV)km$s8_|U}it1vem_|>ds$=t7_"
    b"`7K2Yrx#jy`8qUpt^&9k`AnKBrzoa0`cDxd4$|YN`flCI{1|G3`f!Qh{$>wW`gWSQM9XB``h+*4{uPqO`nnrmR?tkF`o{yA5;PjI"
    b"`tpa=`R&uF`yq=DlY&lD`+$zy&#~&7`_ldh#QqnZ`{I|ME!o;Y{A7Y7n7Uv@{DXD@n=UTs{MA-?Gk|Zd{MS8na>TD<{N8nlS-~Xl"
    b"{O@FX*O@n){PYB6riUK5{TqydIQi=L{Tv=JN~50n{U2{#YB5kv{a0>_#Q{My{g|HiJ9~Nz{igUKC?l3;{jCjjds4-v{piCKstt*g"
    b"{tgB8RBJ>8{w*_TP&dF&{(@Dvm3IbM{*f*@)#G!4{+~@-2wJ`?{-U0fq~lS`{-b!wVMtAs{<5cl?rm>n{^V&t?q^vN{~+>$0<RC7"
    b"|06(Cws1Zb|6&f2GiMHO|AP2sM}6~t|ARY_NWhVh|AfuuWI~n`|LKd8)7rsJ|LYdP0#Bg7|MyJ;ARpZ>"
)
_FRONTEND_FINGERPRINTS = frozenset(
    _FRONTEND_FINGERPRINT_BYTES[i:i + 8] for i in range(0, len(_FRONTEND_FINGERPRINT_BYTES), 8)
)


@pytest.mark.parametrize("name", PAYLOADS)
def test_design_manifest_and_visible_metadata(tmp_path, monkeypatch, name):
    from ouroboros.context import _build_installed_skills_section
    from ouroboros.skill_builtin import install_builtin_skill
    from ouroboros.skill_catalogue import session_skill_index
    from ouroboros.skill_loader import save_enabled

    root = REPO / "skills" / name
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    manifest = parse_skill_manifest_text(text)
    assert manifest.name == name and manifest.version == "1.0.0"
    assert manifest.type == "instruction" and manifest.os == "any"
    assert manifest.permissions == [] and not manifest.entry and not manifest.scripts
    assert not re.search(r"^(entry|scripts)\s*:", text.split("---", 2)[1], re.MULTILINE)
    assert 0 < len(manifest.description) <= 260 and 0 < len(manifest.when_to_use) <= 260
    assert manifest.model_experience.get("what_model_sees") and manifest.model_experience.get("token_effect")
    monkeypatch.setenv("OUROBOROS_TRUST_NATIVE_SEEDED_SKILLS", "true")
    monkeypatch.setattr("ouroboros.config.SETTINGS_PATH", tmp_path / "settings.json")
    assert install_builtin_skill(name, drive_root=tmp_path, seed_dir=REPO / "skills")["ok"]
    save_enabled(tmp_path, name, True, actor="owner_ui")
    ctx = SimpleNamespace(repo_dir=REPO, drive_root=tmp_path)
    for projection in (_build_installed_skills_section(ctx), session_skill_index(ctx)):
        assert manifest.description in projection and manifest.when_to_use in projection


@pytest.mark.parametrize("name", PAYLOADS)
def test_referenced_design_files_are_in_the_bundled_payload(name):
    root = REPO / "skills" / name
    for relative in PAYLOADS[name]:
        assert (root / relative).is_file(), relative
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    paths = set(re.findall(
        r"(?:skills/|(?:\.\./)*)(?:(?:ui_design|design_demo)/)?(?:references|assets)/[\w./-]+", text))
    paths.update(re.findall(r"(?:\.\./)+(?:ui_design|design_demo)/SKILL\.md", text))
    for relative in paths:
        target = (REPO / relative if relative.startswith("skills/") else root / relative).resolve()
        assert target.is_relative_to((REPO / "skills").resolve()) and target.is_file(), relative


def test_design_catalogue_rows_are_recommended_default_on_and_categorised(tmp_path):
    from ouroboros.skill_builtin import default_on_names
    from ouroboros.skill_source_catalog import builtin_snapshot

    seeds = tmp_path / "skills"
    for name in PAYLOADS:
        shutil.copytree(REPO / "skills" / name, seeds / name)
    shutil.copyfile(REPO / "skills/builtin.json", seeds / "builtin.json")
    rows = {row["name"]: row for row in json.loads((seeds / "builtin.json").read_text())["skills"]}
    catalog = {row["name"]: row for row in builtin_snapshot(tmp_path)["entries"]}
    for name in PAYLOADS:
        assert rows[name] == {"name": name, "category": ["design"], "recommended": True, "default_on": True}
        assert catalog[name]["category"] == ["design"] and catalog[name]["recommended"] is True
    assert set(default_on_names(seeds)) == set(PAYLOADS)


@pytest.mark.parametrize("name", PAYLOADS)
def test_design_prose_has_no_eight_word_run_from_the_reference(name):
    root = REPO / "skills" / name
    assert root.is_dir()
    for path in sorted(root.rglob("*")):
        if path.suffix not in {".md", ".js", ".html"}:
            continue
        words = re.findall(r"[a-z0-9]+", path.read_text(encoding="utf-8").lower())
        for i in range(len(words) - 7):
            fingerprint = hashlib.sha256(" ".join(words[i:i + 8]).encode()).digest()[:8]
            assert fingerprint not in _FRONTEND_FINGERPRINTS, f"{path.relative_to(REPO)}: word {i + 1} matches reference"
