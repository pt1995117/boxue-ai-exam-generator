
class RealEstateCalculator:
    """
    存量房买卖相关计算公式集合
    基于《第26届新经纪品牌搏学大考存量房买卖科目考试用书》
    """

    @staticmethod
    def calculate_loan_amount(evaluation_price, loan_ratio):
        """
        Calculate commercial loan amount.
        Use when: bank按评估价核定商业贷款，公式=评估价×贷款成数。
        Preconditions: evaluation_price应不高于成交价；loan_ratio在0-1之间。
        Output: loan amount (same currency unit as evaluation_price).
        """
        return evaluation_price * loan_ratio

    @staticmethod
    def calculate_provident_fund_loan(balance_applicant, balance_co_applicant, multiple, year_coefficient):
        """
        Calculate municipal provident-fund loan limit.
        Use when: 需要按余额×倍数×缴存年限系数测算公积金可贷额度。
        Preconditions: balances为缴存余额；multiple、year_coefficient按政策给定；结果仍需与最高/保底额度取低值。
        Output: eligible provident-fund loan amount.
        """
        return (balance_applicant + balance_co_applicant) * multiple * year_coefficient

    @staticmethod
    def calculate_vat(price, original_price, years_held, is_ordinary, is_residential=True):
        """
        Calculate VAT and surcharges for property transfer.
        Use when: 需要判断是否满足满2且普通住宅免增值税，或差额/全额征收。
        Parameters: price=计税较高值(网签/核定)，original_price=原值，years_held=持有年限，is_ordinary=普通住宅标记，is_residential=是否住宅。
        Notes: 非住宅简化为差额征收；住宅满2且普通住宅免税，否则差额或全额。
        Output: VAT amount.
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
        Calculate deed tax.
        Use when: 需要按面积与首套/二套属性测算契税。
        Parameters: price=计税价, area=建筑面积, is_first_home/is_second_home标记套数, is_residential标记住宅与否。
        Rules: 住宅首套<=140平1%，>140平1.5%；二套<=140平1%，>140平2%；三套及非住宅3%。
        Output: deed tax amount.
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
        Calculate land grant fee for economical housing.
        Use when: 经济适用房转让需补缴土地出让金。
        Rules: 2008-04-11前购买按网签/核定价×10%；之后按(网签/核定价-原购价)×70%。
        Output: fee amount.
        """
        if buy_date_is_before_2008_4_11:
            return price * 0.10
        else:
            return (price - original_price) * 0.70

    @staticmethod
    def calculate_land_grant_fee_managed_economical(price):
        """
        Calculate land grant fee for housing managed as economical housing.
        Use when: 按经适房管理的住房转让，按较高值×3%补缴。
        Output: fee amount.
        """
        return price * 0.03

    @staticmethod
    def calculate_land_grant_fee_public_housing(area, cost_price=1560):
        """
        Calculate land grant fee for purchased public housing (cost method).
        Use when: 已购公房补缴土地出让金，按成本价×面积×1%。
        Parameters: area=建筑面积㎡，cost_price=当年成本价元/㎡(默认1560)。
        Output: fee in yuan.
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
        """
        Calculate remaining land-use years.
        Use when: 出让年限已知，需判断目前剩余年限=总年限-(当前年份-出让年份)。
        Output: remaining years.
        """
        return total_years - (current_year - grant_year)

    @staticmethod
    def calculate_house_age(current_year, completion_year, for_loan=False):
        """
        Calculate house age.
        Use when: 需要房龄用于评估或贷款年限判断。
        Modes: for_loan=False -> 通用房龄=当前年份-竣工年份；for_loan=True -> 贷款计算用房龄=50-(当前年份-竣工年份)以匹配“房龄+贷款年限≤50年”。
        Output: age in years.
        """
        if for_loan:
            # 贷款计算用房龄：房龄 = 50 - (当前年份 - 建成年代)
            return 50 - (current_year - completion_year)
        else:
            # 通用房龄：房龄 = 截止年份 - 房屋竣工年份
            return current_year - completion_year

    @staticmethod
    def calculate_indoor_height(floor_height, slab_thickness):
        """
        Calculate indoor clear height.
        Use when: 层高与楼板厚度已知，需要净高 = 层高 - 板厚。
        Output: meters.
        """
        return floor_height - slab_thickness

    @staticmethod
    def calculate_building_area(inner_area, shared_area):
        """
        Calculate gross floor area.
        Use when: 题干给出套内面积与公摊面积，需要建筑面积=两者之和。
        Output: area in same unit (㎡).
        """
        return inner_area + shared_area

    @staticmethod
    def calculate_efficiency_rate(inner_use_area, building_area):
        """
        Calculate efficiency rate (usable area ratio).
        Use when: 需要得房率=套内使用面积/建筑面积×100%，建筑面积不得为0。
        Output: percentage.
        """
        if building_area == 0: return 0
        return (inner_use_area / building_area) * 100

    @staticmethod
    def calculate_area_error_ratio(registered_area, contract_area):
        """
        Calculate area error ratio.
        Use when: 比较登记/测绘面积与合同面积差异，误差比=(登记-合同)/合同×100%。
        Output: percentage (positive/negative).
        """
        if contract_area == 0: return 0
        return (registered_area - contract_area) / contract_area * 100

    @staticmethod
    def calculate_price_diff_ratio(listing_price, deal_price):
        """
        Calculate price difference ratio (absolute).
        Use when: 对比挂牌价与成交价的偏离度，|挂牌-成交|/成交×100%。
        Output: percentage.
        """
        if deal_price == 0: return 0
        return abs((listing_price - deal_price) / deal_price) * 100

    @staticmethod
    def calculate_plot_ratio(total_building_area, total_land_area):
        """
        Calculate plot ratio (FAR).
        Use when: 需要总建筑面积/总用地面积判断规划合规，土地面积不可为0。
        Output: ratio.
        """
        if total_land_area == 0: return 0
        return total_building_area / total_land_area

    @staticmethod
    def calculate_green_rate(green_area, total_land_area):
        """
        Calculate green coverage rate.
        Use when: 绿化面积/总用地面积×100%，判断是否达标，总用地面积不可为0。
        Output: percentage.
        """
        if total_land_area == 0: return 0
        return (green_area / total_land_area) * 100
