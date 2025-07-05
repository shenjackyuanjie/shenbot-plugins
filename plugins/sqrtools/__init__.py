SQRTOOLS_VERSION="3.3.2"
propname=["HP","攻","防","速","敏","魔","抗","智"]
sklname=["火球","冰冻","雷击","地裂","吸血","投毒","连击","会心","瘟疫","命轮","狂暴","魅惑","加速","减速","诅咒","治愈","苏生","净化","铁壁","蓄力","聚气","潜行","血祭","分身","幻术","防御","守护","反弹","护符","护盾","反击","吞噬","亡灵","垂死","隐匿","空技能","空技能","空技能","空技能","空技能"]
class Name:
    def __init__(self):
        self.__val=[]
        self.namebase:list[int]=[0]*128
        self.namebonus:list[int]=[0]*128
        self.nameprop:list[int]=[0]*8
        self.__sklid=[]
        self.__sklfreq=[]
        self.nameskill:list[tuple[int,int]]=[(0,0)]*16
    def load(self,namein:str)->bool:
        if namein=="" or namein.count('@')>1:
            return False
        namein=list(namein.rpartition('@'))
        if namein[1]=='@':
            if namein[2]=='':
                namein[2]=namein[0]
        else:
            namein[0]=namein[2]
        namestr=list(namein[0].encode())
        teamstr=list(namein[2].encode())
        namestr.insert(0,0)
        teamstr.insert(0,0)
        namelen=len(namestr)
        teamlen=len(teamstr)
        if namelen>256 or teamlen>256:
            return False
        self.__val=list(range(256))
        s=0
        for i in range(256):
            s+=(teamstr[i%teamlen]+self.__val[i])
            s%=256
            self.__val[i],self.__val[s]=self.__val[s],self.__val[i]
        for i in range(2):
            s=0
            for j in range(256):
                s+=(namestr[j%namelen]+self.__val[j])
                s%=256
                self.__val[j],self.__val[s]=self.__val[s],self.__val[j]
        s=0
        for i in range(256):
            m=(self.__val[i]*181+160)%256
            if m>=89 and m<217:
                self.namebase[s]=m&63
                s+=1
        self.namebonus[:]=self.namebase[:]
        return True
    def calcprops(self,usebonus:bool)->None:
        propcnt=1
        if usebonus==True:
            r=self.namebonus[0:32]
        else:
            r=self.namebase[0:32]
        for i in range(10,31,3):
            r[i:i+3]=sorted(r[i:i+3])
            self.nameprop[propcnt]=r[i+1]
            propcnt+=1
        r[0:10]=sorted(r[0:10])
        self.nameprop[0]=154
        for i in range(3,7):
            self.nameprop[0]+=r[i]
        for i in range(1,8):
            self.nameprop[i]+=36
        return
    def calcskill(self,usebonus:bool)->None:
        self.__sklid=list(range(0,40))
        self.__sklfreq=[0]*16
        sklflag=[True,True]
        a=b=0
        randbase=[]
        randbase[:]=self.__val[:]
        def randgen():
            nonlocal a,b,randbase
            def m():
                nonlocal a,b,randbase
                a=(a+1)%256
                b=(b+randbase[a])%256
                randbase[a],randbase[b]=randbase[b],randbase[a]
                return randbase[(randbase[a]+randbase[b])&255]
            return ((m()<<8)|m())%40
        s=0
        for i in range(2):
            for j in range(40):
                s=(s+randgen()+self.__sklid[j])%40
                self.__sklid[j],self.__sklid[s]=self.__sklid[s],self.__sklid[j]
        last=-1
        j=0
        for i in range(64,128,4):
            q=min(self.namebase[i:i+4])
            if usebonus==True:
                p=min(self.namebonus[i:i+4])
            else:
                p=q
            if p>10:
                if self.__sklid[j]<35:
                    self.__sklfreq[j]=p-10
                if q<=10:
                    if j>=14:
                        sklflag[j-14]=False
                elif self.__sklid[j]<25:
                    last=j
            j+=1
        if last!=-1:
            if last>=14:
                sklflag[last-14]=False
            self.__sklfreq[last]*=2
        if usebonus==True:
            info=self.namebonus
        else:
            info=self.namebase
        if self.__sklfreq[14]>0 and sklflag[0]:
            self.__sklfreq[14]+=min(info[60],info[61],self.__sklfreq[14])
        if self.__sklfreq[15]>0 and sklflag[1]:
            self.__sklfreq[15]+=min(info[62],info[63],self.__sklfreq[15])
        self.nameskill=list(zip(self.__sklid[0:16],self.__sklfreq))
        return
