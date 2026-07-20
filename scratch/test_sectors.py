from pykrx import stock

indices = stock.get_index_ticker_list()
for idx in indices[:10]:
    try:
        name = stock.get_index_ticker_name(idx)
        print(idx, name)
    except:
        pass
