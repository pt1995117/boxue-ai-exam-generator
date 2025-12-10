
class RealEstateCalculator:
    """
    存量房买卖相关计算公式集合
    基于《第26届新经纪品牌搏学大考存量房买卖科目考试用书》
    """

    @staticmethod
    def calculate_loan_amount(evaluation_price, loan_ratio):
        """
        计算商业贷款金额
        公式: 贷款金额 = 评估价格 × 贷款成数
        注意: 贷款评估价格不得超过成交价
        """
        return evaluation_price * loan_ratio

    @staticmethod
    def calculate_provident_fund_loan(balance_applicant, balance_co_applicant, multiple, year_coefficient):
        """
        计算市属公积金贷款额度
        公式: (借款申请人余额 + 共同申请人余额) × 倍数 × 缴存年限系数
        注意: 需结合保底额度、最高额度取低值
        """
        return (balance_applicant + balance_co_applicant) * multiple * year_coefficient

    @staticmethod
    def calculate_vat(price, original_price, years_held, is_ordinary, is_residential=True):
        """
        计算增值税及附加
        :param price: 网签价或核定价（较高值）
        :param original_price: 原值
        :param years_held: 持有年限 (>=2 or <2)
        :param is_ordinary: 是否普通住宅
        :param is_residential: 是否住宅
        """
        vat_rate = 0.053 # 增值税及附加税率5.3% (根据原文例题：400÷1.05×5.3%=20.19万元)
        
        if not is_residential:
            # 非住宅通常全额或差额征收，此处简化逻辑，需根据具体非住宅政策完善
            return (price - original_price) / 1.05 * vat_rate

        if years_held >= 2:
            if is_ordinary:
                return 0 # 免征
            else:
                return (price - original_price) / 1.05 * vat_rate
        else:
            return price / 1.05 * vat_rate

    @staticmethod
    def calculate_deed_tax(price, area, is_first_home, is_second_home, is_residential=True):
        """
        计算契税
        :param price: 计税核定价
        :param area: 建筑面积
        :param is_first_home: 是否首套
        :param is_second_home: 是否二套
        """
        if not is_residential:
            return price * 0.03

        if is_first_home:
            if area <= 140:
                return price * 0.01
            else:
                return price * 0.015
        elif is_second_home:
            if area <= 140:
                return price * 0.01
            else:
                return price * 0.02
        else:
            # 三套及以上
            return price * 0.03

    @staticmethod
    def calculate_land_grant_fee_economical(price, original_price, buy_date_is_before_2008_4_11):
        """
        计算经济适用房土地出让金
        :param price: 网签价或核定价（较高值）
        :param original_price: 原购房价格
        :param buy_date_is_before_2008_4_11: 购买时间是否在2008.4.11之前
        """
        if buy_date_is_before_2008_4_11:
            return price * 0.10
        else:
            return (price - original_price) * 0.70

    @staticmethod
    def calculate_land_grant_fee_managed_economical(price):
        """
        计算按经适房管理住房土地出让金
        公式: 较高值 × 3%
        """
        return price * 0.03

    @staticmethod
    def calculate_land_grant_fee_public_housing(area, cost_price=1560):
        """
        计算已购公房土地出让金 (成本价)
        公式: 建筑面积 × 当年成本价格 × 1%
        :param area: 建筑面积（平方米）
        :param cost_price: 当年成本价格（元/平方米），默认1560（城六区成本价）
        :return: 土地出让金（元）
        示例: 61×1560×1%=951.6（元）
        """
        # 确保 cost_price 是数字类型
        if isinstance(cost_price, str):
            # 如果传入字符串，尝试转换为数字，否则使用默认值
            try:
                cost_price = float(cost_price)
            except (ValueError, TypeError):
                cost_price = 1560  # 使用默认值
        return area * float(cost_price) * 0.01

    # --- 通用物理属性计算 ---

    @staticmethod
    def calculate_land_remaining_years(total_years, current_year, grant_year):
        """土地剩余使用年限"""
        return total_years - (current_year - grant_year)

    @staticmethod
    def calculate_house_age(current_year, completion_year, for_loan=False):
        """
        计算房龄
        :param current_year: 截止年份（当前年份）
        :param completion_year: 房屋竣工年份（建成年代）
        :param for_loan: 是否用于贷款计算（公积金/商业贷款）
        :return: 房龄（年）
        
        公式说明：
        - 通用房龄（for_loan=False）: 房龄 = 截止年份 - 房屋竣工年份
          示例: 截止年份=2025, 竣工年份=2010, 房龄=2025-2010=15年
        - 贷款计算用房龄（for_loan=True）: 房龄 = 50 - (当前年份 - 建成年代)
          用于公积金/商业贷款年限计算，基于"房龄+贷款年限≤50年"的约束
          示例: 截止年份=2025, 竣工年份=1993, 房龄=50-(2025-1993)=18年
        """
        if for_loan:
            # 贷款计算用房龄：房龄 = 50 - (当前年份 - 建成年代)
            return 50 - (current_year - completion_year)
        else:
            # 通用房龄：房龄 = 截止年份 - 房屋竣工年份
            return current_year - completion_year

    @staticmethod
    def calculate_indoor_height(floor_height, slab_thickness):
        """室内净高"""
        return floor_height - slab_thickness

    @staticmethod
    def calculate_building_area(inner_area, shared_area):
        """建筑面积"""
        return inner_area + shared_area

    @staticmethod
    def calculate_efficiency_rate(inner_use_area, building_area):
        """得房率"""
        if building_area == 0: return 0
        return (inner_use_area / building_area) * 100

    @staticmethod
    def calculate_area_error_ratio(registered_area, contract_area):
        """面积误差比"""
        if contract_area == 0: return 0
        return (registered_area - contract_area) / contract_area * 100

    @staticmethod
    def calculate_price_diff_ratio(listing_price, deal_price):
        """价差率 (取绝对值)"""
        if deal_price == 0: return 0
        return abs((listing_price - deal_price) / deal_price) * 100

    @staticmethod
    def calculate_plot_ratio(total_building_area, total_land_area):
        """容积率"""
        if total_land_area == 0: return 0
        return total_building_area / total_land_area

    @staticmethod
    def calculate_green_rate(green_area, total_land_area):
        """绿地率"""
        if total_land_area == 0: return 0
        return (green_area / total_land_area) * 100
